/*
 * GB Chat - on-screen keyboard chat for Game Boy (GBDK-2020 4.x),
 * answered by a Z80-uLM model (https://github.com/HarryR/z80ai) running on the console.
 *
 * Screen layout (20x18 tiles)
 *   Background : chat log, redrawn from a RAM buffer.
 *                Received messages on a gray background, sent messages on white.
 *   Window     : status bar (1 row) + input field on gray (4 rows) + keyboard (5 rows),
 *                moved up and down with WY
 *                keyboard shown  : 10 window rows, 8 log rows
 *                keyboard hidden :  5 window rows, 13 log rows
 *   Sprites    : selected key (2 sprites) and text cursor (1 sprite), drawn as negatives.
 *                The 256 background tiles are not enough for white + gray + negative
 *                versions of every character.
 *
 * Status bar : the three layers (abc, ABC, #+=) are always shown as tabs on a black bar,
 * the current one in black on white, the others in white on black, then the character counter.
 *
 * Keyboard layers (layout defined in tools/gen_tiles.py, generated into keyboard.h)
 *   abc, ABC : letters, digits, common punctuation
 *   #+=      : accented letters (capital under lowercase), euro sign, remaining ASCII symbols
 *   Row 4, keys 0 and 1 are the cursor arrows, key 2 is the space bar, on every layer.
 *
 * Controls, keyboard shown
 *   D-pad  : move the key cursor
 *   A      : type the key
 *   B      : delete (auto repeat), or hide the keyboard if the input is empty
 *   Select : next layer (abc, ABC, #+=)
 *   Start  : send the message, then hide the keyboard
 * Controls, keyboard hidden
 *   Up/Down  : scroll the log
 *   Left     : previous sent message (older)
 *   Right    : next sent message (newer)
 *   A/Select : show the keyboard
 *   Start    : send the current input
 */

#include <gb/gb.h>
#include <stdint.h>
#include <string.h>
#include "tiles.h"
#include "lm.h"

/* ---------- Settings ---------- */

#define MAX_LEN      64          /* maximum message length */
#define IN_ROWS      4           /* 4 x 20 = 80 >= 64 */
#define LOG_W        20
#define LOG_MAX      60          /* lines kept in the log (60 x 20 = 1200 bytes) */
#define HIST_N       8           /* sent messages remembered */
#define TEXT_W       18          /* text width in the log (2 columns are used by the prefix) */

#define WIN_ROW_STATUS  0
#define WIN_ROW_INPUT   1
#define WIN_ROW_KB      5
#define WIN_ROWS_KB     10
#define WIN_ROWS_HID    5
#define WY_KB           (144 - 8 * WIN_ROWS_KB)
#define WY_HID          (144 - 8 * WIN_ROWS_HID)
#define VIS_KB          (WY_KB / 8)     /* 8 log rows */
#define VIS_HID         (WY_HID / 8)    /* 13 log rows */

#define REP_DELAY    20          /* frames before auto repeat starts */
#define REP_RATE     4           /* frames between repeats */
#define BLINK_MASK   0x20

#define DEMO         0           /* 1: canned reply instead of the model (interface test) */

/* Sprites */
#define SPR_KEY_L    0
#define SPR_KEY_R    1
#define SPR_CUR      2
#define SPR_SCROLL   3

/* ---------- Keyboard ---------- */

enum { L_LOW = 0, L_UP = 1, L_SYM = 2 };

#include "keyboard.h"                       /* static const char kb[3][51] */

static const char layer_name[3][4] = { "abc", "ABC", "#+=" };

/* ---------- State ---------- */

static uint8_t log_tiles[LOG_MAX][LOG_W];   /* chat log, already as tile indices */
static uint8_t log_count;
static uint8_t scroll;                      /* lines above the bottom */

static char    buf[MAX_LEN + 1];
static uint8_t len, cur;

static char    hist[HIST_N][MAX_LEN + 1];
static uint8_t hist_n, hist_head, hist_pos; /* hist_pos 0 = not browsing */

static uint8_t kb_visible = 1;
static uint8_t layer = L_UP;
static uint8_t kx, ky;                      /* current key */
static uint8_t frame, blink_on = 1;

/* Model reply, displayed as it is generated */
static uint8_t gen_active;
static char    reply[LM_MAX_OUT + 1];
static uint8_t reply_len, reply_lines;

/* ---------- Helpers ---------- */

/* Tile index of a character. base: 0 (white background) or T_GRAY_BASE (gray background).
 * Characters outside ASCII 32..126 are Windows-1252 codes listed in ext_codes. */
static uint8_t tile_of(uint8_t c, uint8_t base) {
    uint8_t k;
    if (c >= 32 && c <= 126) return (uint8_t)(base + c - 32);
    for (k = 0; k < EXT_COUNT; k++)
        if (ext_codes[k] == c) return (uint8_t)(base + 95 + k);
    return (uint8_t)(base + '?' - 32);
}

/* Tab label character, white on black (only the characters of STAT_CHARS exist). */
static uint8_t stat_tile(uint8_t c) {
    static const char chars[] = STAT_CHARS;
    uint8_t i;
    for (i = 0; chars[i]; i++)
        if ((uint8_t)chars[i] == c) return (uint8_t)(T_STAT_BASE + i);
    return T_BLACK;
}

static uint8_t vis_lines(void) {
    return kb_visible ? VIS_KB : VIS_HID;
}

/* ---------- Chat log ---------- */

static void log_push_line(const uint8_t *line) {
    if (log_count == LOG_MAX) {
        memmove(log_tiles[0], log_tiles[1], (LOG_MAX - 1) * LOG_W);
        log_count--;
    }
    memcpy(log_tiles[log_count], line, LOG_W);
    log_count++;
}

static void log_redraw(void) {
    static uint8_t blank[LOG_W];            /* zeros = white space tile */
    uint8_t n = vis_lines(), r;
    uint8_t maxscroll = (log_count > n) ? (uint8_t)(log_count - n) : 0;
    int16_t idx;

    if (scroll > maxscroll) scroll = maxscroll;
    for (r = 0; r < n; r++) {
        idx = (int16_t)log_count - n - scroll + r;
        set_bkg_tiles(0, r, LOG_W, 1, (idx >= 0) ? log_tiles[idx] : blank);
    }
}

/* Appends a message, wrapping at spaces when possible. who: '>' sent, '<' received.
 * Returns the number of log lines added. */
static uint8_t log_add_message(char who, const char *s) {
    uint8_t line[LOG_W];
    uint8_t first = 1, n, k, added = 0;
    uint8_t gray = (who == '<');
    uint8_t base = gray ? T_GRAY_BASE : 0;

    do {
        memset(line, base, LOG_W);          /* space tile of the right color */
        if (first) {
            line[0] = (who == '>') ? T_RARROW : T_LARROW_G;
        }
        n = (uint8_t)strlen(s);
        if (n > TEXT_W) {
            n = TEXT_W;
            for (k = TEXT_W; k > 0; k--) {
                if (s[k] == ' ') { n = k; break; }
            }
        }
        for (k = 0; k < n; k++) line[2 + k] = tile_of((uint8_t)s[k], base);
        s += n;
        while (*s == ' ') s++;
        log_push_line(line);
        added++;
        first = 0;
    } while (*s);

    /* Do not disturb the reader if they scrolled up in the log */
    if (who == '<' && scroll > 0) scroll += added;
    else scroll = 0;
    return added;
}

/* Replaces the reply being generated (its previous lines are removed first) */
static void log_set_reply(const char *s) {
    uint8_t keep = scroll;
    if (reply_lines) log_count -= reply_lines;
    reply_lines = log_add_message('<', s);
    scroll = keep;
}

/* ---------- Sprites: selected key and text cursor ---------- */

static uint8_t win_y(void) {
    return kb_visible ? WY_KB : WY_HID;
}

static uint8_t key_sprite_tile(void) {
    uint8_t c;
    if (ky == 4 && kx == 0) return S_LARROW;
    if (ky == 4 && kx == 1) return S_RARROW;
    c = (uint8_t)kb[layer][ky * 10 + kx];
    if (c == ' ') return S_SPACESYM;
    return tile_of(c, 0);
}

static void place_key_sprites(void) {
    uint8_t y;
    if (!kb_visible) {
        move_sprite(SPR_KEY_L, 0, 0);
        move_sprite(SPR_KEY_R, 0, 0);
        return;
    }
    y = (uint8_t)(win_y() + (WIN_ROW_KB + ky) * 8 + 16);
    set_sprite_tile(SPR_KEY_L, key_sprite_tile());
    set_sprite_tile(SPR_KEY_R, 0);          /* tile 0 = negative space = black block */
    move_sprite(SPR_KEY_L, (uint8_t)(kx * 16 + 8), y);
    move_sprite(SPR_KEY_R, (uint8_t)(kx * 16 + 16), y);
}

static void place_cursor(void) {
    uint8_t c = (cur < len) ? (uint8_t)buf[cur] : ' ';
    set_sprite_tile(SPR_CUR, tile_of(c, 0));
    if (blink_on)
        move_sprite(SPR_CUR, (uint8_t)((cur % 20) * 8 + 8),
                    (uint8_t)(win_y() + (WIN_ROW_INPUT + cur / 20) * 8 + 16));
    else
        move_sprite(SPR_CUR, 0, 0);
}

/* ---------- Window: status bar, input field, keyboard ---------- */

static void place_scroll_sprite(void) {
    if (scroll > 0) {
        set_sprite_tile(SPR_SCROLL, S_SCROLL);
        move_sprite(SPR_SCROLL, (uint8_t)(19 * 8 + 8), (uint8_t)(win_y() + WIN_ROW_STATUS * 8 + 16));
    } else {
        move_sprite(SPR_SCROLL, 0, 0);
    }
}

static void draw_status(void) {
    uint8_t row[20], i, l;
    for (i = 0; i < 20; i++) row[i] = T_BLACK;

    /* the three layers are always shown; the current one is black on white, the others white on black */
    for (l = 0; l < 3; l++) {
        if (l == layer) {
            row[l * 4] = tile_of(' ', 0);
            for (i = 0; i < 3; i++)
                row[l * 4 + 1 + i] = tile_of((uint8_t)layer_name[l][i], 0);
        } else {
            for (i = 0; i < 3; i++)
                row[l * 4 + 1 + i] = stat_tile((uint8_t)layer_name[l][i]);
        }
    }

    /* character counter on a white block; a sprite arrow in the last column shows a scrolled log */
    row[13] = tile_of(' ', 0);
    row[14] = tile_of((uint8_t)('0' + len / 10), 0);
    row[15] = tile_of((uint8_t)('0' + len % 10), 0);
    row[16] = tile_of('/', 0);
    row[17] = tile_of((uint8_t)('0' + MAX_LEN / 10), 0);
    row[18] = tile_of((uint8_t)('0' + MAX_LEN % 10), 0);
    set_win_tiles(0, WIN_ROW_STATUS, 20, 1, row);
    place_scroll_sprite();
}

static void input_redraw(void) {
    uint8_t row[20], r, c, i;
    for (r = 0; r < IN_ROWS; r++) {
        for (c = 0; c < 20; c++) {
            i = (uint8_t)(r * 20 + c);
            row[c] = tile_of((i < len) ? (uint8_t)buf[i] : ' ', T_GRAY_BASE);
        }
        set_win_tiles(0, WIN_ROW_INPUT + r, 20, 1, row);
    }
    blink_on = 1;
    frame = 0;
    place_cursor();
    draw_status();
}

static void draw_key(uint8_t x, uint8_t y) {
    uint8_t c = (uint8_t)kb[layer][y * 10 + x];
    uint8_t t0;
    if (y == 4 && x == 0)      t0 = T_LARROW;
    else if (y == 4 && x == 1) t0 = T_RARROW;
    else if (c == ' ')         t0 = T_SPACESYM;
    else                       t0 = tile_of(c, 0);
    set_win_tile_xy(x * 2, WIN_ROW_KB + y, t0);
    set_win_tile_xy(x * 2 + 1, WIN_ROW_KB + y, 0);
}

static void draw_keyboard(void) {
    uint8_t x, y;
    for (y = 0; y < 5; y++)
        for (x = 0; x < 10; x++)
            draw_key(x, y);
}

static void set_layer(uint8_t l) {
    if (l != layer) {
        layer = l;
        draw_keyboard();
        draw_status();
        place_key_sprites();
    }
}

static void apply_layout(void) {
    move_win(7, win_y());
    log_redraw();
    draw_status();
    place_key_sprites();
    place_cursor();
}

/* ---------- Editing ---------- */

static uint8_t need_cap(void) {
    if (cur == 0) return 1;
    if (cur >= 2 && buf[cur - 1] == ' ' &&
        (buf[cur - 2] == '.' || buf[cur - 2] == '!' || buf[cur - 2] == '?')) return 1;
    return 0;
}

/* Automatic layer: capitals at the start of a sentence, lowercase otherwise.
 * keep_sym: stay on the symbols layer (it is left after a space). */
static void auto_layer(uint8_t keep_sym) {
    if (layer == L_SYM && keep_sym) return;
    set_layer(need_cap() ? L_UP : L_LOW);
}

static void insert_char(char c) {
    uint8_t i;
    if (len >= MAX_LEN) return;
    for (i = len; i > cur; i--) buf[i] = buf[i - 1];
    buf[cur++] = c;
    buf[++len] = 0;
    hist_pos = 0;
    auto_layer(c != ' ');
    input_redraw();
}

static void delete_char(void) {
    uint8_t i;
    if (cur == 0) return;
    for (i = (uint8_t)(cur - 1); i < len - 1; i++) buf[i] = buf[i + 1];
    cur--;
    buf[--len] = 0;
    hist_pos = 0;
    auto_layer(1);
    input_redraw();
}

static void buffer_set(const char *s) {
    strcpy(buf, s);
    len = cur = (uint8_t)strlen(buf);
    auto_layer(0);
    input_redraw();
}

/* ---------- History of sent messages ---------- */

static const char *hist_get(uint8_t k) {   /* k = 1: most recent */
    return hist[(uint8_t)((hist_head + HIST_N - (k - 1)) % HIST_N)];
}

static void hist_push(void) {
    if (hist_n && strcmp(hist_get(1), buf) == 0) return;   /* no consecutive duplicate */
    hist_head = (uint8_t)((hist_head + 1) % HIST_N);
    strcpy(hist[hist_head], buf);
    if (hist_n < HIST_N) hist_n++;
}

static void hist_older(void) {
    if (hist_pos < hist_n) {
        hist_pos++;
        buffer_set(hist_get(hist_pos));
    }
}

static void hist_newer(void) {
    if (hist_pos == 0) return;
    hist_pos--;
    if (hist_pos == 0) buffer_set("");
    else buffer_set(hist_get(hist_pos));
}

/* ---------- Sending and receiving ---------- */

static void chat_transmit(const char *msg) {
#if !DEMO
    /* the model answers: start generating, one character per main loop turn */
    lm_start(msg);
    gen_active = 1;
    reply_len = 0;
    reply_lines = 0;
    reply[0] = 0;
#else
    (void)msg;
#endif
}

#if DEMO
static void chat_receive(const char *msg) {
    log_add_message('<', msg);
    log_redraw();
    draw_status();
}
#endif

static void do_send(void) {
    if (len == 0 || gen_active) return;      /* one message at a time */
    hist_push();
    chat_transmit(buf);
    log_add_message('>', buf);
    hist_pos = 0;
    kb_visible = 0;
    buffer_set("");
    set_layer(L_UP);
    apply_layout();
#if DEMO
    chat_receive("ok, got it");
#else
    log_set_reply("...");                    /* "thinking" indicator */
    log_redraw();
    draw_status();
#endif
}

/* One generation step: one full pass through the network (about 0.7 s on a Game Boy) */
static void gen_tick(void) {
    char c = lm_step();
    if (c == 0) {
        gen_active = 0;
        return;
    }
    reply[reply_len++] = c;
    reply[reply_len] = 0;
    log_set_reply(reply);
    log_redraw();
    draw_status();
}

/* ---------- Input handling ---------- */

static void kb_move(int8_t dx, int8_t dy) {
    kx = (uint8_t)((kx + dx + 10) % 10);
    ky = (uint8_t)((ky + dy + 5) % 5);
    place_key_sprites();
}

static void kb_press(void) {
    char c = kb[layer][ky * 10 + kx];
    if (ky == 4 && kx == 0)      { if (cur > 0)   { cur--; input_redraw(); } }
    else if (ky == 4 && kx == 1) { if (cur < len) { cur++; input_redraw(); } }
    else insert_char(c);
}

static void show_keyboard(void) {
    kb_visible = 1;
    apply_layout();
}

static void handle_visible(uint8_t fire, uint8_t pressed) {
    if (fire & J_LEFT)  kb_move(-1, 0);
    if (fire & J_RIGHT) kb_move(1, 0);
    if (fire & J_UP)    kb_move(0, -1);
    if (fire & J_DOWN)  kb_move(0, 1);

    if (pressed & J_A) kb_press();

    if (fire & J_B) {
        if (len > 0) delete_char();
        else if (pressed & J_B) {            /* a real press only, not an auto repeat */
            kb_visible = 0;
            apply_layout();
        }
    }
    if (pressed & J_SELECT) set_layer((uint8_t)((layer + 1) % 3));
    if (pressed & J_START)  do_send();
}

static void handle_hidden(uint8_t fire, uint8_t pressed) {
    uint8_t n = VIS_HID;
    uint8_t maxscroll = (log_count > n) ? (uint8_t)(log_count - n) : 0;

    if ((fire & J_UP) && scroll < maxscroll) { scroll++; log_redraw(); draw_status(); }
    if ((fire & J_DOWN) && scroll > 0)       { scroll--; log_redraw(); draw_status(); }
    if (fire & J_LEFT)  hist_older();
    if (fire & J_RIGHT) hist_newer();
    if (pressed & (J_A | J_SELECT)) show_keyboard();
    if (pressed & J_START) do_send();
}

/* ---------- Program ---------- */

void main(void) {
    uint8_t keys, prev = 0, pressed, fire, held, rep = 0;
    const uint8_t rmask = J_UP | J_DOWN | J_LEFT | J_RIGHT | J_B;

    DISPLAY_OFF;
    set_bkg_data(0, 128, tiles);                     /* two calls: 256 tiles do not fit a uint8_t count */
    set_bkg_data(128, 128, &tiles[128 * 16]);
    set_sprite_data(0, SPRITE_TILE_COUNT, sprite_tiles);
    fill_bkg_rect(0, 0, 32, 32, 0);
    fill_win_rect(0, 0, 20, 18, 0);
    BGP_REG  = 0xE4;                        /* 0 white, 1 light gray, 2 dark gray, 3 black */
    OBP0_REG = 0xE0;                        /* sprites: color 1 -> white, 3 -> black */
    SPRITES_8x8;

    log_add_message('<', "GB Chat ready.");
    log_add_message('<', "Say hello.");
    draw_keyboard();
    input_redraw();
    apply_layout();
    SHOW_BKG;
    SHOW_WIN;
    SHOW_SPRITES;
    DISPLAY_ON;

    for (;;) {
        wait_vbl_done();
        keys = joypad();
        pressed = (uint8_t)(keys & ~prev);
        fire = pressed;

        /* auto repeat (D-pad and B) */
        held = (uint8_t)(keys & rmask);
        if (held != (uint8_t)(prev & rmask)) {
            rep = 0;
        } else if (held && ++rep >= REP_DELAY) {
            fire |= held;
            rep = REP_DELAY - REP_RATE;
        }
        prev = keys;

        if (kb_visible) handle_visible(fire, pressed);
        else            handle_hidden(fire, pressed);

        if (gen_active) gen_tick();

        /* text cursor blink */
        frame++;
        if (((frame & BLINK_MASK) != 0) == blink_on) {
            blink_on = !blink_on;
            place_cursor();
        }
    }
}
