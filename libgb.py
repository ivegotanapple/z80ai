"""
Game Boy (Sharp SM83 / "GBZ80") utility functions and code builder.

Provides GBBuilder class for emitting machine code for the Game Boy CPU,
mirroring the shape of libz80.py's Z80Builder but for the Game Boy's
instruction set, which differs from a standard Z80 in several ways:

- No IX/IY index registers, no shadow register set (no EXX / EX AF,AF'),
  no I/R refresh registers, no interrupt modes 0/1/2 (only DI/EI + the
  IE/IF memory-mapped registers).
- No IN/OUT: all peripherals (video, sound, joypad, timers...) are
  memory-mapped in the $FF00-$FF7F I/O range.
- 0x10 is STOP here, not DJNZ (the Game Boy has no DJNZ at all).
- Adds LDH (high-page loads/stores at $FF00+n8 or $FF00+C), LD (HL+)/A
  and LD (HL-)/A auto-increment/decrement loads, LD HL,SP+e8, ADD SP,e8,
  and RETI (Z80 needs a separate EI;RET).
- The 0xCB-prefixed table adds SWAP (nibble swap) but is otherwise the
  same shape as Z80: RLC/RRC/RL/RR/SLA/SRA/SWAP/SRL and BIT/RES/SET.
- Several opcodes (0xD3, 0xDB, 0xDD, 0xE3, 0xE4, 0xEB-0xED, 0xF4, 0xFC,
  0xFD) are illegal/undefined and lock up the real CPU; they have no
  mnemonic and are intentionally not exposed here.

Opcode encodings below were verified against the standard SM83 opcode
table (unprefixed + 0xCB-prefixed), not guessed from general Z80 knowledge.
"""

from typing import Dict, List, Tuple, Union

# 8-bit register operand encoding (the 3-bit "r" field). HL means the
# memory operand (HL) - the assembler generates the same opcode either way,
# only the mnemonic differs.
R8 = {'B': 0, 'C': 1, 'D': 2, 'E': 3, 'H': 4, 'L': 5, 'HL': 6, 'A': 7}

# 16-bit register pair encoding for the "rp" field used by LD rp,nn / INC rp
# / DEC rp / ADD HL,rp.
RP16 = {'BC': 0, 'DE': 1, 'HL': 2, 'SP': 3}

# 16-bit register pair encoding for the "rp2" field used by PUSH/POP (uses
# AF instead of SP).
RP16_STK = {'BC': 0, 'DE': 1, 'HL': 2, 'AF': 3}

# Condition code encoding for JR cc / JP cc / CALL cc / RET cc.
CC = {'NZ': 0, 'Z': 1, 'NC': 2, 'C': 3}

# ALU operation encoding for the 0x80-0xBF (op A,r) and 0xC6-0xFE (op A,n)
# blocks.
ALU_OP = {'ADD': 0, 'ADC': 1, 'SUB': 2, 'SBC': 3, 'AND': 4, 'XOR': 5, 'OR': 6, 'CP': 7}

# CB-prefixed rotate/shift group encoding for the 0x00-0x3F block.
CB_ROT_OP = {'RLC': 0, 'RRC': 1, 'RL': 2, 'RR': 3, 'SLA': 4, 'SRA': 5, 'SWAP': 6, 'SRL': 7}

Label = str


class GBBuilder:
    """Simple Game Boy (SM83) code builder with label support"""

    def __init__(self, org: int = 0x0150):
        self.org = org
        self.code = bytearray()
        self.labels: Dict[str, int] = {}
        self.fixups: List[Tuple[int, str, str]] = []  # (offset, label, type)

    def addr(self) -> int:
        return self.org + len(self.code)

    def label(self, name: str):
        self.labels[name] = self.addr()

    def emit(self, *bytes):
        for b in bytes:
            self.code.append(b & 0xFF)

    def emit_word(self, val: int):
        self.emit(val & 0xFF, (val >> 8) & 0xFF)

    def align(self, boundary: int):
        overage = self.addr() % boundary
        if overage < boundary: self.ds(boundary - overage)

    def fixup_word(self, label: str):
        """Emit placeholder word, record fixup"""
        self.fixups.append((len(self.code), label, 'abs'))
        self.emit(0, 0)

    def fixup_rel(self, label: str):
        """Emit placeholder byte for a relative JR"""
        self.fixups.append((len(self.code), label, 'rel'))
        self.emit(0)

    def resolve(self):
        """Apply all fixups"""
        for offset, label, ftype in self.fixups:
            if label not in self.labels:
                raise ValueError(f"Unknown label: {label}")
            target = self.labels[label]

            if ftype == 'abs':
                self.code[offset] = target & 0xFF
                self.code[offset + 1] = (target >> 8) & 0xFF
            elif ftype == 'rel':
                # Relative offset from the address of the byte after the
                # operand (same as Z80's JR/DJNZ timing reference point)
                from_addr = self.org + offset + 1
                rel = target - from_addr
                if rel < -128 or rel > 127:
                    raise ValueError(f"Relative jump out of range: {label} = {rel}")
                self.code[offset] = rel & 0xFF

    def save(self, filename: str):
        self.resolve()
        with open(filename, 'wb') as f:
            f.write(self.code)
        print(f"Wrote {len(self.code)} bytes to {filename}")

    # === Generic table-driven encoders ===
    # The SM83 opcode map is regular enough that most of the instruction
    # set reduces to a handful of formulas; these back the named
    # convenience methods below and can also be used directly.

    def ld_r_r(self, dst: str, src: str):
        """LD dst,src for dst,src in R8. LD (HL),(HL) doesn't exist (it's HALT)."""
        d, s = R8[dst], R8[src]
        if d == 6 and s == 6:
            raise ValueError("LD (HL),(HL) is not a valid instruction (that's HALT)")
        self.emit(0x40 | (d << 3) | s)

    def ld_r_n(self, r: str, n: int):
        self.emit(0x06 | (R8[r] << 3), n & 0xFF)

    def inc_r(self, r: str):
        self.emit(0x04 | (R8[r] << 3))

    def dec_r(self, r: str):
        self.emit(0x05 | (R8[r] << 3))

    def alu_a_r(self, op: str, r: str):
        """ADD/ADC/SUB/SBC/AND/XOR/OR/CP A,r"""
        self.emit(0x80 | (ALU_OP[op] << 3) | R8[r])

    def alu_a_n(self, op: str, n: int):
        """ADD/ADC/SUB/SBC/AND/XOR/OR/CP A,n8"""
        self.emit(0xC6 | (ALU_OP[op] << 3), n & 0xFF)

    def ld_rp_nn(self, rp: str, val: int):
        self.emit(0x01 | (RP16[rp] << 4))
        self.emit_word(val)

    def ld_rp_label(self, rp: str, label: str):
        self.emit(0x01 | (RP16[rp] << 4))
        self.fixup_word(label)

    def inc_rp(self, rp: str):
        self.emit(0x03 | (RP16[rp] << 4))

    def dec_rp(self, rp: str):
        self.emit(0x0B | (RP16[rp] << 4))

    def add_hl_rp(self, rp: str):
        self.emit(0x09 | (RP16[rp] << 4))

    def push(self, rp2: str):
        self.emit(0xC5 | (RP16_STK[rp2] << 4))

    def pop(self, rp2: str):
        self.emit(0xC1 | (RP16_STK[rp2] << 4))

    def jr(self, label: str):
        self.emit(0x18)
        self.fixup_rel(label)

    def jr_cc(self, cc: str, label: str):
        self.emit(0x20 | (CC[cc] << 3))
        self.fixup_rel(label)

    def jp(self, label: str):
        self.emit(0xC3)
        self.fixup_word(label)

    def jp_cc(self, cc: str, label: str):
        self.emit(0xC2 | (CC[cc] << 3))
        self.fixup_word(label)

    def jp_hl(self):
        """JP HL - jump directly to the address in HL (no memory dereference,
        despite the Z80-style "JP (HL)" mnemonic some assemblers use)."""
        self.emit(0xE9)

    def call(self, label: str):
        self.emit(0xCD)
        self.fixup_word(label)

    def call_cc(self, cc: str, label: str):
        self.emit(0xC4 | (CC[cc] << 3))
        self.fixup_word(label)

    def ret(self): self.emit(0xC9)

    def ret_cc(self, cc: str):
        self.emit(0xC0 | (CC[cc] << 3))

    def reti(self): self.emit(0xD9)

    def rst(self, n: int):
        if n not in (0x00, 0x08, 0x10, 0x18, 0x20, 0x28, 0x30, 0x38):
            raise ValueError(f"Invalid RST vector: {n:#x}")
        self.emit(0xC7 | n)

    def _cb(self, opcode: int):
        self.emit(0xCB, opcode & 0xFF)

    def cb_rot(self, op: str, r: str):
        """RLC/RRC/RL/RR/SLA/SRA/SWAP/SRL r"""
        self._cb((CB_ROT_OP[op] << 3) | R8[r])

    def bit(self, b: int, r: str):
        self._cb(0x40 | (b << 3) | R8[r])

    def res(self, b: int, r: str):
        self._cb(0x80 | (b << 3) | R8[r])

    def set(self, b: int, r: str):
        self._cb(0xC0 | (b << 3) | R8[r])

    # === Named convenience wrappers (Z80Builder-style) ===

    def nop(self): self.emit(0x00)
    def halt(self): self.emit(0x76)
    def stop(self): self.emit(0x10, 0x00)
    def di(self): self.emit(0xF3)
    def ei(self): self.emit(0xFB)
    def daa(self): self.emit(0x27)
    def cpl(self): self.emit(0x2F)
    def scf(self): self.emit(0x37)
    def ccf(self): self.emit(0x3F)

    def rlca(self): self.emit(0x07)
    def rrca(self): self.emit(0x0F)
    def rla(self): self.emit(0x17)
    def rra(self): self.emit(0x1F)

    # 16-bit immediate loads
    def ld_bc_nn(self, val): self.ld_rp_nn('BC', val)
    def ld_de_nn(self, val): self.ld_rp_nn('DE', val)
    def ld_hl_nn(self, val): self.ld_rp_nn('HL', val)
    def ld_sp_nn(self, val): self.ld_rp_nn('SP', val)
    def ld_bc_label(self, label): self.ld_rp_label('BC', label)
    def ld_de_label(self, label): self.ld_rp_label('DE', label)
    def ld_hl_label(self, label): self.ld_rp_label('HL', label)
    def ld_sp_label(self, label): self.ld_rp_label('SP', label)

    # 8-bit immediate loads
    def ld_a_n(self, val): self.ld_r_n('A', val)
    def ld_b_n(self, val): self.ld_r_n('B', val)
    def ld_c_n(self, val): self.ld_r_n('C', val)
    def ld_d_n(self, val): self.ld_r_n('D', val)
    def ld_e_n(self, val): self.ld_r_n('E', val)
    def ld_h_n(self, val): self.ld_r_n('H', val)
    def ld_l_n(self, val): self.ld_r_n('L', val)
    def ld_hl_n(self, val): self.ld_r_n('HL', val)  # LD (HL),n8

    # inc/dec 8-bit
    def inc_a(self): self.inc_r('A')
    def inc_b(self): self.inc_r('B')
    def inc_c(self): self.inc_r('C')
    def inc_d(self): self.inc_r('D')
    def inc_e(self): self.inc_r('E')
    def inc_h(self): self.inc_r('H')
    def inc_l(self): self.inc_r('L')
    def dec_a(self): self.dec_r('A')
    def dec_b(self): self.dec_r('B')
    def dec_c(self): self.dec_r('C')
    def dec_d(self): self.dec_r('D')
    def dec_e(self): self.dec_r('E')
    def dec_h(self): self.dec_r('H')
    def dec_l(self): self.dec_r('L')

    # inc/dec 16-bit
    def inc_bc(self): self.inc_rp('BC')
    def inc_de(self): self.inc_rp('DE')
    def inc_hl(self): self.inc_rp('HL')
    def inc_sp(self): self.inc_rp('SP')
    def dec_bc(self): self.dec_rp('BC')
    def dec_de(self): self.dec_rp('DE')
    def dec_hl(self): self.dec_rp('HL')
    def dec_sp(self): self.dec_rp('SP')

    def add_hl_bc(self): self.add_hl_rp('BC')
    def add_hl_de(self): self.add_hl_rp('DE')
    def add_hl_hl(self): self.add_hl_rp('HL')
    def add_hl_sp(self): self.add_hl_rp('SP')

    # push/pop
    def push_bc(self): self.push('BC')
    def push_de(self): self.push('DE')
    def push_hl(self): self.push('HL')
    def push_af(self): self.push('AF')
    def pop_bc(self): self.pop('BC')
    def pop_de(self): self.pop('DE')
    def pop_hl(self): self.pop('HL')
    def pop_af(self): self.pop('AF')

    # register-to-register loads (subset commonly needed; ld_r_r covers the rest)
    def ld_a_b(self): self.ld_r_r('A', 'B')
    def ld_a_c(self): self.ld_r_r('A', 'C')
    def ld_a_d(self): self.ld_r_r('A', 'D')
    def ld_a_e(self): self.ld_r_r('A', 'E')
    def ld_a_h(self): self.ld_r_r('A', 'H')
    def ld_a_l(self): self.ld_r_r('A', 'L')
    def ld_b_a(self): self.ld_r_r('B', 'A')
    def ld_c_a(self): self.ld_r_r('C', 'A')
    def ld_d_a(self): self.ld_r_r('D', 'A')
    def ld_e_a(self): self.ld_r_r('E', 'A')
    def ld_h_a(self): self.ld_r_r('H', 'A')
    def ld_l_a(self): self.ld_r_r('L', 'A')

    # (HL)/(BC)/(DE) memory loads
    def ld_a_hl(self): self.ld_r_r('A', 'HL')   # LD A,(HL)
    def ld_hl_a(self): self.ld_r_r('HL', 'A')   # LD (HL),A
    def ld_b_hl(self): self.ld_r_r('B', 'HL')
    def ld_c_hl(self): self.ld_r_r('C', 'HL')
    def ld_d_hl(self): self.ld_r_r('D', 'HL')
    def ld_e_hl(self): self.ld_r_r('E', 'HL')

    def ld_a_bc(self): self.emit(0x0A)   # LD A,(BC)
    def ld_bc_a(self): self.emit(0x02)   # LD (BC),A
    def ld_a_de(self): self.emit(0x1A)   # LD A,(DE)
    def ld_de_a(self): self.emit(0x12)   # LD (DE),A

    # HL auto-increment/decrement (Game Boy-specific)
    def ld_hl_inc_a(self): self.emit(0x22)   # LD (HL+),A
    def ld_a_hl_inc(self): self.emit(0x2A)   # LD A,(HL+)
    def ld_hl_dec_a(self): self.emit(0x32)   # LD (HL-),A
    def ld_a_hl_dec(self): self.emit(0x3A)   # LD A,(HL-)

    # Direct 16-bit addressing
    def ld_a_mem_label(self, label: str):
        self.emit(0xFA)
        self.fixup_word(label)

    def ld_mem_label_a(self, label: str):
        self.emit(0xEA)
        self.fixup_word(label)

    def ld_mem_label_sp(self, label: str):
        """LD (a16),SP"""
        self.emit(0x08)
        self.fixup_word(label)

    # High-page I/O loads/stores (Game Boy-specific: accesses $FF00+n)
    def ldh_n_a(self, n: int):
        """LDH ($FF00+n8),A"""
        self.emit(0xE0, n & 0xFF)

    def ldh_a_n(self, n: int):
        """LDH A,($FF00+n8)"""
        self.emit(0xF0, n & 0xFF)

    def ldh_c_a(self):
        """LDH ($FF00+C),A"""
        self.emit(0xE2)

    def ldh_a_c(self):
        """LDH A,($FF00+C)"""
        self.emit(0xF2)

    # Stack-pointer arithmetic (Game Boy-specific)
    def add_sp_e(self, e: int):
        """ADD SP,e8 (signed)"""
        self.emit(0xE8, e & 0xFF)

    def ld_hl_sp_e(self, e: int):
        """LD HL,SP+e8 (signed)"""
        self.emit(0xF8, e & 0xFF)

    def ld_sp_hl(self): self.emit(0xF9)
    def jp_hl_direct(self): self.jp_hl()

    # ALU shorthands
    def add_a_n(self, val): self.alu_a_n('ADD', val)
    def adc_a_n(self, val): self.alu_a_n('ADC', val)
    def sub_n(self, val): self.alu_a_n('SUB', val)
    def sbc_a_n(self, val): self.alu_a_n('SBC', val)
    def and_n(self, val): self.alu_a_n('AND', val)
    def xor_n(self, val): self.alu_a_n('XOR', val)
    def or_n(self, val): self.alu_a_n('OR', val)
    def cp_n(self, val): self.alu_a_n('CP', val)

    def add_a_r(self, r): self.alu_a_r('ADD', r)
    def adc_a_r(self, r): self.alu_a_r('ADC', r)
    def sub_r(self, r): self.alu_a_r('SUB', r)
    def sbc_a_r(self, r): self.alu_a_r('SBC', r)
    def and_r(self, r): self.alu_a_r('AND', r)
    def xor_r(self, r): self.alu_a_r('XOR', r)
    def or_r(self, r): self.alu_a_r('OR', r)
    def cp_r(self, r): self.alu_a_r('CP', r)

    def or_a(self): self.alu_a_r('OR', 'A')
    def xor_a(self): self.alu_a_r('XOR', 'A')  # common A=0 idiom
    def and_a(self): self.alu_a_r('AND', 'A')
    def cp_a(self): self.alu_a_r('CP', 'A')

    # Conditional jumps/calls/returns
    def jr_nz(self, label): self.jr_cc('NZ', label)
    def jr_z(self, label): self.jr_cc('Z', label)
    def jr_nc(self, label): self.jr_cc('NC', label)
    def jr_c(self, label): self.jr_cc('C', label)
    def jp_nz(self, label): self.jp_cc('NZ', label)
    def jp_z(self, label): self.jp_cc('Z', label)
    def jp_nc(self, label): self.jp_cc('NC', label)
    def jp_c(self, label): self.jp_cc('C', label)
    def call_nz(self, label): self.call_cc('NZ', label)
    def call_z(self, label): self.call_cc('Z', label)
    def call_nc(self, label): self.call_cc('NC', label)
    def call_c(self, label): self.call_cc('C', label)
    def ret_nz(self): self.ret_cc('NZ')
    def ret_z(self): self.ret_cc('Z')
    def ret_nc(self): self.ret_cc('NC')
    def ret_c(self): self.ret_cc('C')

    # CB-prefixed shift/rotate shorthands (rarely all needed; call cb_rot
    # directly for less common register/op combinations)
    def swap_a(self): self.cb_rot('SWAP', 'A')
    def swap_hl(self): self.cb_rot('SWAP', 'HL')  # SWAP (HL)
    def sla_a(self): self.cb_rot('SLA', 'A')
    def sra_a(self): self.cb_rot('SRA', 'A')
    def srl_a(self): self.cb_rot('SRL', 'A')
    def rl_a(self): self.cb_rot('RL', 'A')
    def rr_a(self): self.cb_rot('RR', 'A')
    def rlc_a(self): self.cb_rot('RLC', 'A')
    def rrc_a(self): self.cb_rot('RRC', 'A')

    # Data
    def db(self, *vals):
        for v in vals:
            self.emit(v)

    def dw(self, *vals):
        for v in vals:
            self.emit_word(v & 0xFFFF)

    def ds(self, n):
        for _ in range(n):
            self.emit(0)

    def ascii(self, s):
        for c in s:
            self.emit(ord(c))


# The Nintendo logo bitmap that must appear at $0104-$0133 of every
# cartridge header. The original boot ROM compares it and refuses to boot
# the game (with the screen showing a broken logo) if it doesn't match;
# most emulators enforce this too.
NINTENDO_LOGO = bytes.fromhex(
    "CEED6666CC0D000B03730083000C000D0008111F8889000EDCCC6EE6DDDDD9"
    "9999BBBB67636E0EECCCDDDC999FBBB9333E"
)


def build_gb_header(entry_jump_label_addr: int, title: str = "", cart_type: int = 0x00,
                     rom_size: int = 0x00, ram_size: int = 0x00) -> bytes:
    """
    Build the 80-byte Game Boy cartridge header ($0100-$014F).

    entry_jump_label_addr: address to JP to at $0150 (i.e. where code
        actually starts, right after this header).
    title: up to 15 ASCII characters (byte at $0143 is reserved for the
        CGB flag and left 0 here, i.e. DMG-only compatibility).

    Returns 80 bytes to be placed starting at $0100. The caller is
    responsible for placing $0000-$00FF (interrupt vectors + boot jump)
    before it and the actual code at $0150 (see entry_jump_label_addr).
    """
    h = bytearray(80)  # $0100-$014F

    # $0100-$0103: entry point - NOP; JP entry_jump_label_addr
    h[0] = 0x00  # NOP
    h[1] = 0xC3  # JP
    h[2] = entry_jump_label_addr & 0xFF
    h[3] = (entry_jump_label_addr >> 8) & 0xFF

    # $0104-$0133: Nintendo logo
    h[4:4 + len(NINTENDO_LOGO)] = NINTENDO_LOGO

    # $0134-$0142: title (15 bytes for DMG; $0143 left as CGB flag = 0)
    title_bytes = title.encode('ascii')[:15]
    h[0x34:0x34 + len(title_bytes)] = title_bytes

    # $0144-$0145: new licensee code (unused, "00")
    h[0x44] = ord('0')
    h[0x45] = ord('0')
    # $0146: SGB flag (0 = no SGB support)
    h[0x46] = 0x00
    # $0147: cartridge type (0 = ROM ONLY)
    h[0x47] = cart_type
    # $0148: ROM size code, $0149: RAM size code
    h[0x48] = rom_size
    h[0x49] = ram_size
    # $014A: destination code (0 = Japanese, 1 = non-Japanese)
    h[0x4A] = 0x01
    # $014B: old licensee code (0x33 signals "check new licensee code")
    h[0x4B] = 0x33
    # $014C: mask ROM version number
    h[0x4C] = 0x00

    # $014D: header checksum over bytes $0134-$014C
    checksum = 0
    for b in h[0x34:0x4D]:
        checksum = (checksum - b - 1) & 0xFF
    h[0x4D] = checksum

    # $014E-$014F: global checksum, filled in later by finalize_gb_rom
    # once the full ROM is known (left as 0 here).
    h[0x4E] = 0x00
    h[0x4F] = 0x00

    return bytes(h)


def finalize_gb_rom(rom: bytearray) -> bytearray:
    """
    Compute and patch in the global checksum at $014E-$014F (sum of every
    byte in the ROM except those two, big-endian). Most emulators don't
    check this, but real hardware and some tooling do.
    """
    total = sum(b for i, b in enumerate(rom) if i not in (0x14E, 0x14F)) & 0xFFFF
    rom[0x14E] = (total >> 8) & 0xFF
    rom[0x14F] = total & 0xFF
    return rom
