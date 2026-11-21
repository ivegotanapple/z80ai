# GB Chat + Z80-μLM
---
## DISCLAIMER: THIS IS AI GENERATED SLOP
I have not written a single line of code on this repository. You can assume its garbage.
The original repo is written with claude too. I'm not in position to judge. 

It works as far as I've tested. Build instruction were also wrote by claude.

<img width="241" height="240" alt="Capture d&#39;écran 2026-10-24 114814" src="https://github.com/user-attachments/assets/59b1f196-8e42-4924-b9e3-d9995f87a796" />
<caption>As you can see it produces very reliable statements (ironic)</caption>

<img width="241" height="240" alt="Capture d&#39;écran 2026-11-21 214721" src="https://github.com/user-attachments/assets/30c32734-40d4-4037-abd5-3e059a99347e" />
<caption>8bits generated nonsense</caption>

---
A chat interface for the Game Boy (GBDK-2020 4.x) answered by a z80ai model
(https://github.com/HarryR/z80ai) that runs on the console itself.

## Build

    make GBDK=/path/to/gbdk              # produces gbchat.gb (64 KB, MBC1)
    make model MODEL=model.npz           # switch model (model.npz from z80ai's exportmodel.py)
    make tiles                           # after editing LAYOUT in tools/gen_tiles.py

The model in `model/` comes from z80ai's `examples/tinychat` (z80ai is MIT or Apache-2.0).

## Controls

Keyboard shown: D-pad moves the key cursor, A types, B deletes (hides the keyboard when the
input is empty), Select cycles the layers, Start sends and hides the keyboard.
Keyboard hidden: Up/Down scrolls the log, Left/Right browses the sent messages,
A or Select shows the keyboard, Start sends.

## Keyboard

Three layers, always visible as tabs in the status bar (the current one is highlighted):

- `abc` and `ABC`: digits, letters, `' , . ? / : ( ) ; ! -`
- `#+=`: accented letters (capital under lowercase), the euro sign and the ASCII symbols
  that are not on the other layers. Nothing is repeated.

Row 4, keys 0 to 2 are the same on every layer: cursor left, cursor right, space.

Accented characters on the symbols layer (26): `é è ê à ù ç œ æ ß ñ` / `É È Ê À Ù Ç Œ Æ € Ñ` /
`ä ö ü` / `Ä Ö Ü`. They are stored as Windows-1252 codes. The model was trained on ASCII, so
before hashing they are folded to their base letter (`é` becomes `e`).

## Limits

- Background tiles: 256 of 256 are used (95 ASCII + 26 extended characters, each in white and
  gray, plus the status bar and arrows). Adding one more character means giving up another.
- Sprite tiles: 125 of 128.
- Not available: `¡ ¿ ° ® † × ∞ © § £ θ ≃` and other Latin-1 letters such as `â î ô û ë ï á í ó ú å ø`.

## Contents

- `main.c`, `tiles.c/h`, `keyboard.h`: interface (tiles and keyboard are generated)
- `lm.c`, `lm.h`, `lm_kernel.s`: inference engine (C and an SM83 assembly kernel)
- `model/`: weights and biases as C sources, one layer per ROM bank
- `tools/gbexport.py` (model to C), `tools/gen_kernel.py` (assembly kernel),
  `tools/gen_tiles.py` (tiles and keyboard layout)
- `tests/`: Python reference (`ref_infer.py`), outputs of the real Z80 binary (`z80_ref.json`),
  `run_lmtest.py` (engine against the reference, plus accent folding), `e2e.py` (full ROM with
  button presses)

## Tests

    pip install pyboy numpy
    python3 tests/run_lmtest.py --gbdk /path/to/gbdk --model model.npz
    python3 tests/e2e.py --model model.npz

The engine output is identical, character for character, to the real CHAT.COM Z80 binary on
31 prompts. Everything was tested in the PyBoy emulator only, not on hardware.

## Performance

About 0.67 s per generated character (0.45 s in the kernel, 0.22 s in C around it). A reply
such as "HI" takes 3 passes (2 characters and the end marker), "MAYBE" takes 6. The buttons are
not read during a pass.
