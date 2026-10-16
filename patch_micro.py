#!/usr/bin/env python3
"""
Patch bitbox micro 2.3.4: MIDI control of things only the encoders reach.

Two voice classes handle per-pad Control Change, each with its own struct
layout, stack frame and return target. Both are hooked.

  class A  FUN_080b10b8   Sample, Granular
  class B  FUN_080b74ec   Clip, Slicer

A multi-sample pad is a Sample pad with a folder loaded into it, so it is
class A too. Looper is neither, and has no FX sends at all.

Also rewrites the startup splash: line 2 stops crediting 1010music, and the
version is marked as a modified build.

Usage: patch_micro.py <in.bin> <out.bin> [version]
"""
import ast, collections, hashlib, os, struct, subprocess, sys, tempfile

BASE = 0x08040000
STOCK_SHA = "54ecc3e5c09b9aed7ad8e2d9d1c9f8ee63e06a8351c857304c3ff21fcb9c4365"
STOCK_SIZE = 671376
TAPER = 0x080a04c0          # pure dB->linear taper, float in/out
CC_MAX = 16383              # CC values are 14-bit in this firmware
# --- splash screen -------------------------------------------------------
# Three consecutive 16-byte slots in the product string table:
#   0x080c5750  "bitbox micro"   line 1, names the hardware -- left alone
#   0x080c5760  "by 1010music"   line 2, attribution -- must not survive
#   0x080c5770  "2.3.4"          line 3, version
# A pointer table starts at 0x080c5780, so a slot overrun corrupts real data.
# 15 chars + terminator each.
SLOT = 16
ATTRIB_VA, ATTRIB_STOCK = 0x080c5760, "by 1010music"
ATTRIB_NEW = "community fw"
VER_VA, VER_STOCK = 0x080c5770, "2.3.4"

# The slot STORES 15 characters plus a terminator. The panel DISPLAYS fewer, and
# we have no figure for that from documentation -- only from an incident.
#
# 2026-09-05: `2.3.7-mod-b10` is thirteen characters and was read back off the
# module as `2.3.7-mod-b1`, which is exactly twelve. Every earlier EQ build was
# twelve or fewer (`2.3.7-mod-b7`) and read correctly. That cost real time: two
# hardware faults were reported against "B1" and had to be traced back to which
# image was actually on the card before either could be investigated.
#
# One observation is not a specification, so this limit is NOT set at the
# observed edge. Ten characters leaves two to spare against the only evidence we
# have, and `2.3.7-b10` fits in nine -- so the scheme from b11 onward drops the
# `-mod` infix, which the `community fw` line above already says.
#
# CONFIRMED END TO END ON HARDWARE, 2026-09-05. b11 is the first build in the
# nine-character scheme and its splash was read back in full -- "all characters,
# nothing cut". String chosen, guard enforced here at build time, legible on the
# panel. But read the evidence for what it is: ONE build at NINE characters
# displaying fully proves nine fits. It does not measure the panel, so 10 is
# still a conservative choice made from a single truncation observation rather
# than a measured limit. Do not raise it on the strength of b11.
#
# This is a guard, not a convention, for the reason plan 01 gave: a rule you have
# to remember is a rule you will forget at 2am with a card in your hand.
VER_DISPLAY_MAX = 10

# (label, hook VA, return VA, cc-value stack slot, delay off, reverb off)
# --- CC map ---------------------------------------------------------------
# Grouped by what each control affects, and within each group the preset-owned
# CCs come first and the firmware-owned ones after:
#
#    70-78   pad, preset    the nine per-pad lanes the app writes
#    79-93   pad, firmware  this patch, read-only, costs no modulation slot
#    94-95   pad, firmware  reserved for Mute and Solo if those hunts land
#   96-101   UNUSABLE       MIDI reserves these (data entry, NRPN, RPN)
#  102-107   FX, preset     the six global FX destinations the app writes
#  108-109   FX, firmware   this patch
#  110-111   FX, firmware   this patch, delay Filter and Width
#  120/123   stop           the standard MIDI messages, per channel
#
# LFO Rate is deliberately absent. The manual marks it "Mod Target? Yes", so it
# is reachable from a preset and never needed a hook here. It reads as one
# control on the device but is two parameters underneath -- lforate when Beat
# Sync is off, lforatebeatsync when it is on -- and both take a modsource. The
# Delay time control is built the same way and behaves the same way.
CC_GDENSITY, CC_GSIZE, CC_GWINDOW, CC_GSCATTER, CC_GPANRND = 79, 80, 81, 82, 83
CC_DELAY, CC_REVERB, CC_REVERSE = 84, 85, 86
CC_SUSTAIN, CC_LOOPMODE, CC_LOOPFADE, CC_PLAYTHRU = 87, 88, 89, 90
CC_LFOWAVE, CC_LFOBEATSYNC, CC_LFOTRIG = 91, 92, 93
CC_BEATSYNC, CC_PINGPONG = 108, 109
CC_DLYFILTER, CC_DLYWIDTH = 110, 111
CC_STOP_HARD, CC_STOP_SOFT = 120, 123   # All Sound Off / All Notes Off

# --- granular parameters -------------------------------------------------
# The five Gran-page controls the module will not let you modulate. The manual
# marks them "Mod Target? No" and they have no three-box indicator on the
# device, so the hardware encoder is the only thing that reaches them. Speed is
# absent deliberately: it IS modulatable and MIDI Learn binds it fine.
#
# These are NOT written directly. Each parameter's setter does real work --
# Scatter indexes a lookup table, Grain Size clamps against 0x348, Window
# calls a converter and offsets by 0x100, Density triggers a recompute -- so
# reimplementing any of it would be five chances to be subtly wrong.
#
# Instead the hook lands in the firmware's own code. The per-pad CC handler and
# the set-parameter handler are the SAME function sharing one stack frame, and
# every parameter case reads its value from the same slot the CC arrives in
# ([sp,#0x40]) and exits via the same branch. So an arm only has to rescale the
# value in place and jump to the case. The firmware does the rest, including
# the recompute, exactly as it does for the encoder.
#
# Posting a set-parameter message instead would deadlock: the queue blocks past
# 32 entries and this function is both producer and consumer.
# case entry -> first instruction there, checked before we trust the address
# Every case we jump into, with the first four bytes found there in stock
# 2.3.4. These are branches into the middle of a function, so landing on the
# wrong instruction would corrupt a pad rather than fail cleanly -- the patcher
# checks each site before writing anything.
CASES = {
    #  CC              case VA     guard word   param  what the case expects
    CC_GDENSITY:    (0x080b30a2, 0x7a10eddd),  # 0x160  0..1000, then a recompute
    CC_GSIZE:       (0x080b3108, 0x7a10eddd),  # 0x15c  0..1000, mod 0x348, clamp
    CC_GWINDOW:     (0x080b2ab8, 0x7a10eddd),  # 0x3e   0..1000, converter, clamp
    CC_GSCATTER:    (0x080b30e6, 0x49aa9b10),  # 0x15e  0..1000, table lookup
    CC_GPANRND:     (0x080b314a, 0x7a10eddd),  # 0x15f  0..1000, straight scale
    CC_SUSTAIN:     (0x080b2bee, 0x7a10eddd),  # 0x22   0..1000, /1000 to a float
    CC_LOOPFADE:    (0x080b2fd0, 0x7a10eddd),  # 0xdb   0..1000, /1000 to a float
    CC_LOOPMODE:    (0x080b31a0, 0xf8c49b10),  # 0x70   raw 0/1/2, plain store
    CC_LFOWAVE:     (0x080b2f2e, 0xf5049b10),  # 0x8    raw 0..9, it adds 1
    CC_LFOBEATSYNC: (0x080b2938, 0xf8d49a10),  # 0xc    zero / non-zero
    CC_LFOTRIG:     (0x080b2ed6, 0xf8d49a10),  # 0xd    zero / non-zero
}

# How many discrete values each enum control has, for even CC banding.
ENUM_STEPS = {CC_LOOPMODE: 3, CC_LFOWAVE: 10}

# Stopping a pad. Class A case 0x32 and class B's shared 0x32/0x46 handler both
# stop whatever the pad is playing WITHOUT testing the message key -- and we are
# already inside the target pad's handler when a CC arrives, so branching there
# stops exactly that pad. No key to look up, no value to convert.
STOP_A, STOP_A_GUARD = 0x080b209c, 0x2b006ee3
STOP_B, STOP_B_GUARD = 0x080b77ee, 0x3260f894

# --- reaching class B's own parameter setter ----------------------------
# Class A can branch into a case because its CC handler and its setter are one
# function. Class B's setter is separate (FUN_080b6ce8) and virtually
# dispatched, so there is no case to jump into and no static caller to copy.
#
# Calling it is possible because its argument surface is tiny -- across the
# whole switch it reads param_2+0x18, param_3+0xc (the parameter id) and
# param_3+0x10 (the value), and nothing else. param_4 is touched by two cases
# we do not use.
#
# The two pointers are both live at the hook. FUN_080b74ec opens with
# "mov r4, r0" and "ldr.w sb, [r1]", so r4 is the voice and r9 is *param_2 --
# the same object class A calls iVar14 and reads its timestamp from. r9 is
# written once, in that prologue, and not again in the 748 instructions before
# the hook, so it still holds it when a CC arrives.
#
# This is how the three LFO controls are reached. Writing their fields by hand
# was the alternative and a bad one: each case ends by calling a resync
# (FUN_080ba214) that takes that timestamp, and without it the value lands
# somewhere the LFO never reads.
CLASSB_SETPARAM, CLASSB_SETPARAM_GUARD = 0x080b6ce8, 0x41f0e92d
CLASSB_MSG = 0x18            # scratch message frame; 8-aligned, id at +0xc
P_LFOWAVE, P_LFOBEATSYNC, P_LFOTRIG = 0x8, 0xc, 0xd

# A Control Change arrives widened to 14-bit as (7-bit value << 7) -- the LSB
# is discarded, so only 128 values are reachable however the controller sends
# it. Shift back to 0..127, then scale to the 0..1000 the parameters use.
# 8063/1024 == 7.87402, and 127*8063 >> 10 == 1000 exactly, so both endpoints
# land clean; scaling the raw 14-bit by 1000/16383 would top out at 992.
CC7_TO_1000_MUL, CC7_TO_1000_SHIFT = 8063, 10

# --- global delay toggles ------------------------------------------------
# The delay FX object has no Control Change handler at all; MIDI reaches FX
# parameters through the modulation matrix, which computes base +/- amount and
# so cannot drive a boolean. Instead we post the same set-parameter message the
# FX screen knobs use, from the MIDI dispatcher (the control thread, which is
# where engine commands are meant to be posted from).
MIDI_POST_CALL = 0x08093f14   # 'bl FUN_080950c0' that posts the CC event
MIDI_POST_FN   = 0x080950c0   # the function it calls
MSG_CHAN_SP    = 0x80         # MIDI channel 0-15, caller-frame offset
MSG_CC_SP      = 0x84         # CC number,   caller-frame offset
MSG_VAL_SP     = 0x88         # 14-bit value, caller-frame offset
SETPARAM_FN    = 0x080937f4   # cmd39: (manager, padKey, paramId, value, 0, 0)
MANAGER        = 0x24022978
DELAY_KEY      = 0x30000      # delay cell = layer 3, col 0, row 0 (reverb 0x30001)
P_BEATSYNC, P_PINGPONG = 0x17, 0x18
# The delay's own filter switch and bandwidth. Ghidra's decompile of the delay
# object silently drops the stretch of the switch these live in, which is why
# they looked unreachable for a while -- but the comparisons are plainly there
# in the disassembly at DLYFX_GUARD_VA, and they are registered alongside
# cutoff/res/delay/feedback so they belong to this object. Filter is 0/1;
# Width is 0..1000 like every other continuous FX parameter.
P_DLYFILTER, P_DLYWIDTH = 0xab, 0xac
DLYFX_GUARD_VA, DLYFX_GUARD = 0x080a7ca8, 0xf0002bab   # 'cmp r3,#0xab'
BOOL_THRESHOLD = 8192          # half of 16383: >= is "on"

# --- the compressor (new in 2.3.6) ---------------------------------------
# The module has a compressor across Out 1 and 2. It ships, it works, and the
# only control the UI gives you is On/Off -- everything else is compiled in.
#
# Unlike every other control here, these are DIRECT FIELD WRITES. The
# compressor object has no set-parameter handler at all, so there is no setter
# to hand a value to. That is safe only because each field is consumed raw:
# the gain computer reads threshold and ratio out of the object every block,
# and nothing is derived or cached from them. See COMPRESSOR-DESIGN.md.
#
# The addresses are RAM, so they cannot be checked directly. Each one is
# instead proved by an instruction in the image that computes it, and those
# are guarded below -- an offset is only as trustworthy as its proof.
# The session is NOT at a fixed address. MANAGER is the engine; the session
# hangs off it as a POINTER, and everything the compressor needs is an offset
# from that pointer's target. Reading it at runtime is the only correct way --
# treating MANAGER as the session lands ~150 KB away, in memory belonging to
# something else entirely.
# CC 40 writes the on/off flag on the SESSION, which is allocated once at boot
# and is therefore a safe target. CCs 41-45 do NOT write the engine at all --
# they write the patch's own store, and hook E stamps that onto whichever
# compressor object the audio is actually processing, every block. That is what
# makes the values survive: hardware testing showed that a direct
# write to the compressor object is undone after a variable period.
# The compressor block listens on ONE channel, unlike every other patched
# global. CC 108-111 get away with answering on all sixteen because they sit in
# 102-119, which the MIDI spec genuinely leaves undefined. 40-45 do not: CC 32-63
# are the fine-adjust LSBs of CC 0-31, so CC 40 is formally the LSB of CC 8.
# Almost nothing sends those, which is why the range looked free -- but gear does
# help itself to the numbers, and a patched global consumes a CC on
# every channel. A stray CC 45 at value 0 sets makeup to -36 dB, which is silence
# and reads exactly like a firmware fault. Confirmed on hardware.
COMP_CHANNEL   = 0            # 0 = MIDI channel 1

CC_COMP_ONOFF  = 40
CC_COMP_FIRST  = 41           # 41 thresh, 42 ratio, 43 attack, 44 release, 45 makeup
CC_COMP_COUNT  = 5
COMP_ONOFF_OFF = 0x28cc0      # session -> on/off byte

# The patch's own parameter store. Chosen from a 60 KB run of AXI SRAM with no
# literal references anywhere in the image, and below the two MPU regions at
# 0x24060000/0x24070000 which are configured for DMA. UNVERIFIED -- "no literal
# references" does not prove "not reached by pointer arithmetic". The magic word
# means a clobber degrades to the feature switching itself off rather than
# stamping garbage into the audio path. See COMPRESSOR-DESIGN.md milestone M1.
# Where the patch keeps its five parameters. This has to be memory the module
# does not use, and "no code mentions it" is NOT enough -- a heap hands out
# addresses it computes at runtime. Beta 4 put the store at 0x24054000, which
# is two-thirds of the way UP the heap; it worked until the heap grew into it,
# then allocations ate the tail of the store and the compressor went silent.
#
#   0x2402e128  heap start   \  malloc's own bounds, read out of its
#   0x24068aa8  heap end     /   literal pool at 0x080bcbe4 / 0x080bcbe0
#   0x240692a8  initial SP -- ARM stacks are full-descending, so nothing
#               above this is ever stack (vector table word 0)
#   0x24080000  end of AXI SRAM
#
# So the 93 KB above the stack pointer is above the heap AND above the stack.
# Nothing in the image references it, and a DMA engine cannot target a buffer
# whose address appears nowhere. The guards below pin all three boundaries, so
# a firmware that moves any of them fails the patch instead of corrupting RAM.
HEAP_LO_VA, HEAP_LO   = 0x080bcbe4, 0x2402e128   # malloc's heap start
HEAP_HI_VA, HEAP_HI   = 0x080bcbe0, 0x24068aa8   # malloc's heap end
STACK_VA,   STACK_TOP = 0x08040000, 0x240692a8   # vector table word 0 = initial SP
SRAM_END              = 0x24080000

PATCH_RAM   = 0x2406c000      # above heap end and above the stack top
PATCH_MAGIC = 0x1010c0de      # +0 magic, +4 thresh, +8 ratio, +12 atk, +16 rel, +20 makeup
PATCH_SUM   = 24              # +24 checksum = magic XOR all five values

# What the compressor comes up as. 1010music ship -4 dB / 4:1 / 10 ms / 250 ms
# / -4 dB, which is a headroom trim rather than a musical setting -- it exists
# so the stock module cannot clip itself, not because it sounds like anything.
# Since the patch has to own these five values anyway, it may as well start
# somewhere useful: gentle bus glue. Every one stays adjustable, and the guide
# gives the numbers to put the stock voicing back on a button.
# Three of these five are literally SSL bus-compressor switch positions -- 2:1,
# 30 ms, 0.3 s -- which is the classic mix-glue setting. The threshold is the one
# the SSL does not give you a number for either: on that unit you turn it until
# the meter shows 2-4 dB of reduction. -12 dB puts us there at sensible levels,
# does less rather than more if the module is run cold, and cannot crush at 2:1
# even if it is badly wrong. Makeup stays at 0 dB so switching the compressor on
# can only ever reduce the level, never clip someone.
DEFAULTS = [
    (struct.unpack("<I", struct.pack("<f", -12.0))[0], "threshold -12.0 dB   70%"),
    (struct.unpack("<I", struct.pack("<f",   2.0))[0], "ratio       2:1       5%"),
    (1440,                                             "attack     30 ms     30%"),
    (14400,                                            "release   300 ms     29%"),
    (struct.unpack("<I", struct.pack("<f",   0.0))[0], "makeup      0.0 dB   50%"),
]
DEFAULT_SUM = PATCH_MAGIC
for _w, _n in DEFAULTS:
    DEFAULT_SUM ^= _w

assert PATCH_RAM > HEAP_HI,   "patch store must sit above malloc's heap"
assert PATCH_RAM > STACK_TOP, "patch store must sit above the initial stack pointer"
assert PATCH_RAM + PATCH_SUM + 4 <= SRAM_END, "patch store must fit in AXI SRAM"

# Field offsets on the compressor object, proved by its own two constructors.
COMP_F_THRESH, COMP_F_RATIO   = 0x428, 0x42c
COMP_F_MAKEUP                 = 0x430
COMP_F_ATTACK, COMP_F_RELEASE = 0x448, 0x44c

# (scale, offset, is_integer) per CC, for a 0..CC_MAX input
COMP_RANGES = [
    (-40.0,     0.0,   False),   # 41 threshold, dB
    (  1.0,    20.0,   False),   # 42 ratio
    ( 24.0,  4800.0,   True ),   # 43 attack,  samples (0.5 .. 100 ms @ 48 k)
    (480.0, 48000.0,   True ),   # 44 release, samples (10 ms .. 1 s @ 48 k)
    (-36.0,    36.0,   False),   # 45 makeup, dB
]

# Finding the session for CC 40. Not by arithmetic -- scan the engine's pointer
# slots and PROVE each candidate by reading back the safety limiter's two timing
# constants. Nothing else in memory carries 192 then 36000 at those offsets, and
# nothing on the module ever writes them.
SCAN_LO, SCAN_HI = MANAGER + 0xa000, MANAGER + 0xa400
RAM_LO, RAM_SPAN = 0x24000000, 0x50000     # every deref is range-checked first
PROOF_A_OFF, PROOF_A_VAL = 0xffa0, 192     # safety-stage attack,  samples
PROOF_R_OFF, PROOF_R_VAL = 0xffa4, 36000   # safety-stage release, samples

# Image guards. The RAM offsets above are only as trustworthy as the
# instructions that prove them, and those are all in the image.
COMP_DISP_VA, COMP_DISP = 0x0809704c, 0x3038f64f   # movw r0,#0xfb38
COMP_FLAG_VA, COMP_FLAG = 0x08097030, 0x00028cc0   # the on/off flag's offset
COMP_INIT_VA, COMP_INIT = 0x080b9d88, 0x6085f504   # add.w r0,r4,#0x428
COMP_DFLT_VA, COMP_DFLT = 0x080b9dc0, 0xc0800000   # the -4.0 default
# movw r8,#0xa204 -- proves where the session pointer is kept on the engine
COMP_SESS_VA, COMP_SESS = 0x08094146, 0x2804f24a

# hook E: the one call site where the engine hands us the live compressor
COMP_DSP_CALL, COMP_DSP_CALL_GUARD = 0x08097056, 0xfd55f022
COMP_DSP_FN = 0x080b9b04

# ---------------------------------------------------------------- the EQ
# 2.3.7-mod. Everything below is read out of the stock image -- see
# EQ-RE-FINDINGS.md, which records how each address was proved.
#
# The EQ node is a live chain node that already runs once per audio block,
# already fetches the audio handle and the frame count, and then never asks for
# the buffers. The arm supplies the arithmetic that is missing.

# The panel is fixed and the vocabulary follows it (D-01): four encoders, each
# with a push button. Encoder 1 selects the mode and its push is master bypass;
# encoders 2-4 are the generic performance controls A, B and C, and their
# pushes are buttons A, B and C. Each mode declares what its three knobs mean.
# Nothing reads a control called sweep, shape or second sweep any more -- those
# names existed only while modes were assumed to need differing control counts.
#
# CC 50-58 sit in the 32-63 fine-adjust LSB range, which other gear on the same
# bus helps itself to. That is exactly why they are gated to channel 1, the
# same exception already established for the compressor at CC 40-45 -- and one
# that cost a hardware session to learn (D-04). Not to be revisited.
EQ_CHANNEL   = 0              # channel 1 only -- CC 50-58 are LSBs, see hook D
CC_EQ_FIRST  = 50             # the whole EQ block, and the only two numbers
CC_EQ_LAST   = 59             # hook F's bounds check cares about
CC_EQ_MODE = 50                                     # mode select, encoder 1
CC_EQ_A, CC_EQ_B, CC_EQ_C = 51, 52, 53              # controls A/B/C, encoders 2-4
CC_EQ_BTN_A, CC_EQ_BTN_B, CC_EQ_BTN_C = 54, 55, 56  # their pushes, binary (D-03)
CC_EQ_BYPASS   = 57           # master bypass, encoder 1's push
CC_EQ_SLOPE    = 59           # filter slope 12 <-> 24 dB/oct, its own button.
                              # Applies to EVERY filter mode, which is why it is
                              # a control of its own and not DJ Filter's button B
                              # as it was through b5.
# CC 58 is RETIRED. It was the self test (D-09), which existed to prove OUR
# biquad was transparent -- and b5 deleted that biquad, so it proved nothing the
# stock handler does not already guarantee. It duplicated master bypass audibly
# and it was a stage hazard: the flag lived in the patch store, not the preset,
# so a preset reload could not clear it and only a power cycle could. Hook F
# still accepts CC 58 because it falls inside the 50..59 window, but its slots
# entry points at EQ_S_SCRATCH, so it writes a dead word and does nothing.
# 59-63 are spare and sit outside CC_EQ_LAST, so hook F ignores them entirely.

# The class, from the constructor's own literal pool.
EQ_VTABLE      = 0x080e259c   # in flash; the ctor stores this address into obj+0
EQ_VT_HANDLER  = EQ_VTABLE + 0x0c
EQ_HANDLER     = 0x080bb220   # what that slot holds, and our tail-branch target

# Object layout. Coefficients are (b2, b1, b0, a1, a2) -- NOT (b0, b1, b2, ...).
EQ_CUR_OFF     = 0x2c         # smoothed coefficients -- what the DSP must read
EQ_STATE_OFF   = 0x54         # 4 floats: left z1/z2 at +0x54/+0x58, right at
                              # +0x5c/+0x60. THE STOCK FILTER'S OWN HISTORY --
                              # it loads these before its sample loop and stores
                              # them after. The ctor zeroes them and nothing
                              # else ever does, not even the stock skip-band
                              # path, which only advances its pointers.
EQ_BAND_STRIDE = 0x38
EQ_PARM_OFF    = 0x10c        # per band, stride 0x14
EQ_TYPE_OFF    = EQ_PARM_OFF          # byte
EQ_GAIN_OFF    = EQ_PARM_OFF + 4      # float, in DECIBELS -- 10^(g/20) inside
EQ_FREQ_OFF    = EQ_PARM_OFF + 8      # float, in HZ -- the designer does 2*pi*f/SR
EQ_Q_OFF       = EQ_PARM_OFF + 0xc    # float, ctor default 1.0
EQ_EN_OFF      = EQ_PARM_OFF + 0x10
EQ_PARM_STRIDE = 0x14

# design(r0 = obj, r1 = band), indexed by type: 0 None, 1 L Cut, 2 L Shelf,
# 3 Param, 4 H Shelf, 5 H Cut.
EQ_DESIGNERS = [0, 0x080ba794, 0x080ba8d8, 0x080bab98, 0x080baf5c, 0x080bae18]
EQ_T_LCUT, EQ_T_LSHELF, EQ_T_PARAM, EQ_T_HSHELF, EQ_T_HCUT = 1, 2, 3, 4, 5

# NOT USED, and deliberately kept as a headstone. The whole DSP tail this arm
# used to carry -- buffer fetch, biquad, filter state, pole and finiteness
# tests -- existed because EQ-RE-FINDINGS.md concluded the stock EQ handler
# "never asks for the buffers ... that one call is the entire gap". It was
# wrong. The EQ's buffer fetch is 0x080a3228; the RE searched for the
# COMPRESSOR's 0x080a3288, one transposed digit away, did not find it, and
# invented a redundant DSP for us to write. The stock handler at 0x080bb504 is
# already a complete two-channel transposed-direct-form-II biquad, gated on the
# band's type at +0x10c and enable at +0x11c -- which is to say, gated on
# exactly the two fields `gsetband` writes.
#
# Worse than redundant: our biquad kept its state in the band's own state words
# at +0x54 and +0x5c, which is where the stock filter keeps ITS state. Two
# recursive filters sharing one state array inside the same block destroyed each
# other's history, and the master bus went silent whenever any band was enabled.
# Hardware-confirmed 2026-09-04: deleting our DSP made every mode and every
# control work. Do not reintroduce a per-sample loop here. Set parameters,
# chain to the stock handler, and let 1010music filter.
# EQ_AUD_HANDLE / EQ_AUD_FRAMES / EQ_AUD_BUFS were 0x080a82b8 / 0x080a31e0 /
# 0x080a3288 respectively.

# The EQ's own block in the patch store, above the compressor's.
#
# Hook F bumps `gen` on every accepted CC. Hook G re-applies the whole mode on
# EVERY audio block, unconditionally, exactly as hook E stamps the compressor's
# store onto its live object every block.
#
# It used to apply only when `gen` differed from `agen`, and that economy gave
# away the property that makes hook E robust. `gen` tracks OUR intent; it says
# nothing about the state of the object. A preset load reconstructs the EQ
# object to constructor defaults while the store still says `agen == gen`, so
# the compare took its early exit forever and the object stayed configured by
# nobody -- audio passing, nothing happening, and structurally invisible to a
# null test, because failure exits to pass-through and pass-through is exactly
# what such a test expects.
#
# The cost of applying every block is BOUNDED, not assumed: 2,512 instructions
# and 5,444 cycles in a 1,280,000-cycle block, 0.43%. D-25's identity fill is
# what makes that tractable -- `gapply` designs exactly four bands on every
# call, in every mode, so the worst case IS the typical case and there is no
# tail. The derivation and its counts are in DEV-README.
EQ_STORE  = PATCH_RAM + 0x40
EQ_MAGIC  = 0x1010e00e
# Ten live words, one per accepted CC, all checksummed. Three bookkeeping
# words that are not. And one dead scratch word, which exists so hook F's
# unreachable slots-table entries have somewhere harmless to point.
EQ_S_MODE  = 0x04             # CC 50, already divided down to a slot number
EQ_S_GA    = 0x08             # CC 51, control A, raw 0..16256
EQ_S_GB    = 0x0c             # CC 52, control B
EQ_S_GC    = 0x10             # CC 53, control C
EQ_S_BTN_A = 0x14             # CC 54, button A, 0 or 1
EQ_S_BTN_B = 0x18             # CC 55, button B
EQ_S_BTN_C = 0x1c             # CC 56, button C
EQ_S_BYP   = 0x20             # CC 57, master bypass, 0 or 1
EQ_S_SLOPE = 0x24             # CC 59, filter slope, 0 = 12 dB/oct, 1 = 24.
                              # Reuses the retired self test's slot rather than
                              # renumbering the store: same width, same checksum
                              # arithmetic, one fewer thing to get wrong.
EQ_S_GEN   = 0x28             # bumped by every accepted CC
EQ_S_SUM   = 0x2c             # the checksum, deliberately outside EQ_S_LIVE
EQ_S_AGEN  = 0x30             # the generation hook G last applied. RECORDED,
                              # not a gate: hook G applies every block. Kept
                              # because a later plan may want a cheap "did
                              # anything change" signal for something other
                              # than skipping the apply.
EQ_S_MASK  = 0x34             # which of the four bands the mode built
EQ_S_SCRATCH = 0x38           # dead, and NOT in EQ_S_LIVE -- see hook F's slots
EQ_S_LIVE = (EQ_S_MODE, EQ_S_GA, EQ_S_GB, EQ_S_GC,
             EQ_S_BTN_A, EQ_S_BTN_B, EQ_S_BTN_C,
             EQ_S_BYP, EQ_S_SLOPE, EQ_S_GEN)
EQ_STORE_END = EQ_STORE + 0x3c
assert EQ_STORE >= PATCH_RAM + 0x20, "EQ store must clear the compressor's"
assert EQ_STORE_END <= SRAM_END, "EQ store must fit in AXI SRAM"

# BUG-01, promoted from a bug into a check. A duplicated constants block once
# bound EQ_S_SUM to 0x14 -- the same offset as EQ_S_GA, Control A's slot -- so
# the checksum was written into a word it checksums and could never agree with
# itself. Hook G took its pass-through exit on every audio block and the EQ
# never engaged, on any mode, on any CC, for the whole of 2.3.7-mod-b2. The
# four below refuse that shape of mistake at import, before a build can start.
# They are cheap and they are not noise; leave them in.
assert len(set(EQ_S_LIVE)) == len(EQ_S_LIVE), \
    "EQ_S_LIVE names the same store offset twice, so the checksum folds that " \
    "word in twice and cancels it. Check the nine EQ_S_* offsets above."
assert EQ_S_SUM not in EQ_S_LIVE, \
    f"EQ_S_SUM ({EQ_S_SUM:#x}) is inside EQ_S_LIVE, so the checksum is stored " \
    "in a word it checksums and can never agree with itself -- hook G would " \
    "exit to pass-through on every audio block and the EQ would never engage. " \
    "This is BUG-01. Give EQ_S_SUM an offset of its own, outside EQ_S_LIVE."
assert len({*EQ_S_LIVE, EQ_S_SUM, EQ_S_AGEN, EQ_S_MASK, EQ_S_SCRATCH}) \
        == len(EQ_S_LIVE) + 4, \
    "one of EQ_S_SUM / EQ_S_AGEN / EQ_S_MASK / EQ_S_SCRATCH sits on a live " \
    "word or on another bookkeeping word. Every store offset must be distinct " \
    "-- two names on one address means one of them is silently overwritten at " \
    "runtime, and EQ_S_SCRATCH landing on a live word would turn hook F's dead " \
    "slots-table entries into real writes that re-checksum the store."
assert EQ_STORE_END - EQ_STORE >= \
    max(EQ_S_LIVE + (EQ_S_SUM, EQ_S_AGEN, EQ_S_MASK, EQ_S_SCRATCH)) + 4, \
    "EQ_STORE_END stops short of the highest store offset, so the fits-in-SRAM " \
    "assert above does not cover every word the store actually writes. Raise " \
    "EQ_STORE_END to at least the top offset plus four."

# Modes. mode = (7-bit value * 10) >> 7, so each of the ten slots is about 12.8
# encoder values wide. The controller sends ten steps over Min 6 / Max 122,
# which centres every step in its window with SIX values of margin either side
# -- the widest margin this design has ever had, because fewer modes means
# wider windows. A full 0..127 sweep would land a step within one value of a
# boundary, which is what made dialling a mode unreliable through b5.
#
# TWO NUMBERINGS, and every confusion in this feature has come from mixing
# them. The enum below is ZERO-based. The dial, and every table in
# EQ-CONTROL-LAYOUT.md, is ONE-based. Both columns are in the table so that
# neither document has to be translated in somebody's head.
#
# A push earns a CC only when it does something the controller cannot. The
# Drop's `Reset Mid` already gives instant neutral on any encoder, so a button
# that merely returns a control to its resting value is redundant. What
# survives is the momentary maximum: held, the value is forced; released, the
# encoder's stored position comes back untouched, with no jump. Every push
# below is that gesture, and the slots that are blank are blank on purpose.
#
# ALL TEN ARE BUILT. The last column is the build each one first shipped in,
# which is more use than a bare "yes" and cannot rot the way a NOT BUILT marker
# does -- four of them were still claiming NOT BUILT three builds after they
# went on the module.
#
# enum dial  mode           A          B          C          pushes: 54 / 55 / 56                      built
# ----------------------------------------------------------------------------------------------------------
#   0    1   DJ Filter      filter     resonance  knee       -            / -              / -               b5
#   1    2   Dual Cut       high cut   low cut    resonance  open HC      / open LC        / -               b5
#   2    3   Band Pass      centre     width      gain       -            / open widest    / -               b5
#   3    4   Mixer EQ       high       mid        low        kill high    / kill mid       / kill low        b5
#   4    5   Tone + Filter  high       low        filter     kill high    / kill low       / filter open     b13
#   5    6   Tilt           tilt       hinge      -          full treble  / -              / -               b12
#   6    7   Notch          frequency  Q          depth      -            / -              / slam depth      b5
#   7    8   Peak           frequency  Q          gain       -            / max Q          / max boost       b12
#   8    9   Multi Notch    sweep      depth      spread     -            / max depth      / widest spread   b14
#   9   10   Formant        vowel      intensity  shift      -            / max intensity  / -               b15
#
# TEN, and it is final (D-26). Three slots went, for three different reasons.
#
#   `Off` went at b6. CC 57 is a working master bypass and did the job better,
#   so a slot spent on it was a dead encoder position.
#
#   Two mid/side modes went at b10, and they are IMPOSSIBLE rather than merely
#   unwritten (D-27). Both need a mid/side matrix, which is per-sample
#   arithmetic on the raw buffers, and D-19 forbids that absolutely. There is
#   no way round it in band parameters either: the EQ object loads ONE
#   coefficient set per band and applies it to BOTH channels -- only the
#   recursive state is per-channel -- so band parameters cannot express a
#   left-right difference at any setting. Do not re-open this, and do not
#   propose a per-sample loop for it.
#
#   The tone-shaping mode recorded as the fallback for the slot those two
#   vacated went with them (D-28). Tilt already covers tone shaping, and ten
#   modes that each earn their place beat eleven with a passenger.
#
#   Neither the two nor the fallback is named here, on purpose. A name in a
#   comment is a name a future session will try to build, and this table is the
#   first place it would look. EQ-CONTROL-LAYOUT.md carries the record of what
#   was dropped and why; that is the one place any of them should appear.
#
# Nothing renumbered when twelve became ten: the two that went sat past every
# other slot, so every built mode kept its value and `gapply`'s five compares
# were untouched. EQ-CONTROL-LAYOUT.md is the authority here (D-31) and it
# supersedes D-08 and MASTER-EQ-DESIGN.md section 5.
(EQ_M_DJ, EQ_M_DUALCUT, EQ_M_BANDPASS, EQ_M_MIXER, EQ_M_TONE,
 EQ_M_TILT, EQ_M_NOTCH, EQ_M_PEAK, EQ_M_MULTINOTCH, EQ_M_FORMANT) = range(10)
# SLOT 8 IS CALLED **PHASER** ON THE PANEL. The internal name stays
# MULTINOTCH -- and so do `gmmnot`, `gmnsp`, `gqmn` and the EQ_MN_ constants --
# for two reasons, neither of them inertia. First, b14 is flashed and has a
# hardware verdict against sha256 f0c1a144..., and the safest way to keep that
# verdict describing a rebuildable image is not to touch the emitting source at
# all. Second, `ph` is already Band Pass's: `gmphone`, `gqph` and
# EQ_PHONE_HALF are the telephone band, and a `gqphs` one character away from
# `gqph` is a collision waiting to be misread. Internal names differing from
# panel names is already this file's normal -- it says "control A" where the
# panel says "encoder 2". D-15 renamed the SLOT from Phaser to Multi Notch on a
# DSP argument, which still holds (see `gmmnot`); the performer renamed it back
# on an audible one, which is a different question and theirs to settle.
EQ_M_SLOTS = 10               # and the divisor: mode = (value * 10) >> 7
EQ_BOOT_MODE = EQ_M_DJ        # slot 0, and the boot mode

EQ_CC_MAX    = 127 << 7       # 16256, what the arm can actually be sent
EQ_CENTRE    = EQ_CC_MAX // 2 # 8128
EQ_DETENT    = 128            # one 7-bit step either side of centre
EQ_HALF_SPAN = EQ_CENTRE - EQ_DETENT

# The values a button substitutes for a control's stored one (D-03). They are
# in the RAW CC DOMAIN on purpose, exactly as if the encoder had been turned
# there: a forced value goes on to take the same gu16 / gftab / gqmap / ggain
# path the encoder's value would have taken, so a mode cannot accidentally give
# its forced case a different curve from its swept case, and the sweep that
# already covers the control range already covers the forced value too.
# Naming them beats repeating the literals, because "why 8128" is the question
# somebody asks in six months and EQ_FORCE_CENTRE answers it in the name.
EQ_FORCE_CENTRE = EQ_CENTRE   # the detent: flat gain, or a real filter bypass
EQ_FORCE_MAX    = EQ_CC_MAX   # the top of a control: filter open, widest
EQ_FORCE_MIN    = 0           # the bottom: fully closed, zero depth, no tilt

# Sweep range. The low end is NOT arbitrary: a two-pole section's stability
# margin is 2(1-cos w)/(1+alpha), which collapses as the corner approaches DC --
# 114 float32 ULP at 40 Hz, 29 at 20 Hz, 7 at 10 Hz. The top is the designer's
# own clamp: it compares 2*pi*f/SR against pi/2, so SR/4 is the ceiling.
EQ_F_LO, EQ_F_HI = 40.0, 12000.0
EQ_TBL_N = 16                 # 17 entries; worst linear-interp error 1.6%

# Band Pass half-width, in gftab table steps. EQ_BP_WIDE is half the table, so
# a centred width control puts both edges at or past the ends of the range and
# gftab clamps them there -- which is what makes centre transparent (Rule 1).
# EQ_BP_NARROW is the tightest the clockwise end reaches; it is deliberately
# not zero, because a zero-width band pass is two coincident cuts and that is
# exactly the silence Dual Cut used to produce.
# DJ Filter's variable knee, in gftab table steps: how far the second section
# is spread from the first at the anticlockwise end of control C. Four steps is
# a little over an octave and a half on this table, which is enough to hear the
# transition soften without the second section wandering out of the audible
# part of the sweep.
EQ_DJ_SPREAD = 4.0

EQ_BP_WIDE   = 8.0
EQ_BP_NARROW = 0.4
EQ_FTABLE = [EQ_F_LO * (EQ_F_HI / EQ_F_LO) ** (i / EQ_TBL_N) for i in range(EQ_TBL_N + 1)]

EQ_Q_LO, EQ_Q_HI       = 0.707, 8.0     # the filter modes' resonance
EQ_QN_LO, EQ_QN_HI     = 1.0, 20.0      # the notch, which wants to get narrower
EQ_Q_FIXED             = 0.707          # cascaded second section, and the shelves
EQ_Q_BELL              = 0.9            # mixer mid, and the telephone band edges

# Mixer EQ gain, asymmetric: full cut to a near-kill, limited boost, flat at
# centre. Two linear segments so the centre detent really is 0 dB.
EQ_G_CUT, EQ_G_BOOST = -24.0, 9.0

# Tilt's gain law is SYMMETRIC where ggain is deliberately not, and the reason
# is what a tilt control is FOR. Its two shelves have to cancel: equal and
# opposite is the whole point, so a clockwise tilt lifts the treble by exactly
# what it drops the bass, and the total loudness stays roughly put instead of
# swelling. Reuse ggain and the two halves stop cancelling -- -24 dB of bass
# against +9 dB of treble is not a tilt, it is a bass cut with a garnish, and
# every position off centre would read as a volume change.
#
# 9.0 dB, not some new number: it is EQ_G_BOOST's own ceiling, so Tilt
# introduces no extreme this patch has not already swept, designed and played.
# The magnitude is Claude's Discretion under D-17 and the sweep covers whatever
# it is set to; what is NOT discretionary is the symmetry.
EQ_TILT_G = 9.0
EQ_MIX_F  = (150.0, 700.0, 4000.0)      # low shelf, mid bell, high shelf
EQ_NOTCH_G   = -24.0

# Multi Notch, called PHASER on the panel. Four notches swept together, sharing
# one depth and one Q, spaced by a fixed number of gftab table steps.
#
# ONE Q FOR ALL FOUR, and fixed because there is no fourth encoder: the panel is
# sweep, depth and spread, and every one of them earns its place. 4.0 is narrow
# enough that four of them read as notches rather than as a general dulling, and
# wide enough that they stay audible while the stack is moving.
EQ_MN_Q = 4.0

# The spread range, in table steps. `gftab`'s table is LOGARITHMIC, so a fixed
# number of steps is a fixed RATIO between neighbours -- which is what keeps the
# spacing geometric and stops it ever being a comb's arithmetic harmonic series.
# One step is (12000/40)^(1/16) = 1.4283.
#
#   spread, steps | neighbour ratio | nearest simple interval | distance
#   --------------|-----------------|-------------------------|----------
#   0.35          | 1.132           | about two semitones     | --
#   1.0           | 1.428           | tritone, 1.414          | +17 cents
#   1.675 (rest)  | 1.817           | between a minor 7th and an octave | dissonant
#   1.944         | 2.000           | THE OCTAVE              | the comb zone
#   3.0           | 2.914           | octave and a fifth, 3:1 | -50 cents
#
# `gqmap` is linear over the whole control range, so the resting spread is the
# midpoint, 1.675 steps -- a neighbour ratio of 1.817, which is genuinely
# dissonant and is the position most likely to read as a moving sweep. The
# octave, the setting most likely to read as a STATIC TUNED COMB, sits about
# 60% clockwise: reachable on purpose, never where the knob rests.
#
# The low end is deliberately NOT zero. Four coincident notches are one deeper
# notch pretending to be four, which is the same category of mistake as Dual
# Cut's zero-width pass band.
#
# No offline gate has an opinion on any of this. R2 and the smoother-path check
# are numerical stability tests with no concept of musical consonance, so where
# this range reads as a sweep and where as a comb is an ears-only judgement.
EQ_MN_SP_LO, EQ_MN_SP_HI = 0.35, 3.0

# Formant, called FORMANT on the panel (slot 9, dial position 10). Three bells
# parked on the first three resonances of a human vocal tract, morphing
# continuously along one path through vowel space.
#
# THESE FIFTEEN NUMBERS ARE THE ONLY CONSTANTS IN THIS FILE THAT COME FROM
# OUTSIDE THIS PROJECT. Every other number here was either read out of the
# stock image or chosen against something the repository can measure. These are
# measurements of human speakers, and NOTHING in the simulator, the R2 sweep,
# the emitted-code gate or the smoother-path check can tell a right one from a
# wrong one. A mistyped formant produces a mode that sweeps convincingly and
# does not sound like speech, which at the bench is indistinguishable from a DSP
# bug. So the provenance is written down at the same length as the numbers.
#
# --- SOURCE -----------------------------------------------------------------
#
# Peterson, G. E. and Barney, H. L. (1952), "Control Methods Used in a Study of
# the Vowels", Journal of the Acoustical Society of America 24(2), 175-184.
#
# The rows below were NOT copied from a secondary table. They were COMPUTED, on
# 2026-09-05, from that study's own measurements: `verified_pb.data` in the CMU
# Artificial Intelligence Repository's `areas/speech/database/pb/` package,
#
#   www.cs.cmu.edu/afs/cs/project/ai-repository/ai/areas/speech/database/pb/pb.tgz
#
# which is 1,520 rows -- 76 speakers x 10 vowels x 2 repetitions -- of speaker
# class, speaker id, vowel, F0, F1, F2 and F3 in Hz. It reached CMU from the
# University of Pennsylvania and is the file documented in Watrous, JASA 89
# (May 1991). Each row below is the MEAN OVER THE 33 MEN, sixty-six utterances
# per vowel, rounded to the nearest 10 Hz.
#
# The method is recorded rather than only the citation, so anybody who doubts a
# number can regenerate the whole table in four lines instead of arguing about
# it: filter the file to class 1, group by the ARPABET column, average columns
# 6, 7 and 8.
#
# --- HOW THE VOWELS ARE NAMED HERE ------------------------------------------
#
# THIS FILE IS PURE ASCII and IPA is not, so every vowel below is named three
# ways that are all unambiguous: the dataset's own ARPABET code, the keyword
# Peterson and Barney recorded it in, and the letter this mode's knob shows.
# ARPABET is the one to trust in an argument -- it is what the data file
# itself says, and it needs no transcription convention.
#
#   knob  ARPABET  keyword
#   I     IY       heed
#   E     EH       head
#   A     AA       hod
#   O     AO       hawed
#   U     UW       who'd
#
# --- WHAT WAS CHECKED AGAINST WHAT ------------------------------------------
#
# The computed means agree with the average-male table that is quoted
# second-hand throughout the literature, within one 10 Hz rounding step on every
# value except one:
#
#   vowel        computed here        commonly quoted      difference
#   IY  heed     270  2290  2940      270  2290  3010      F3, 70 Hz (2.4%)
#   EH  head     530  1850  2480      530  1840  2480      F2, 10 Hz
#   AA  hod      720  1090  2440      730  1090  2440      F1, 10 Hz
#   AO  hawed    570   840  2400      570   840  2410      F3, 10 Hz
#   UW  who'd    310   880  2240      300   870  2240      F1, 10 Hz
#
# The one real gap is /i/'s third formant, and it is the least load-bearing
# number in the table: F3 of the brightest vowel, a 6 dB bell 2.4% off a
# frequency the shift control moves by an octave either way. The computed value
# ships, because it is the one derived from the primary data.
#
# --- THE LABEL DISCREPANCY, AND HOW IT WAS RESOLVED -------------------------
#
# The phase research found a course PDF whose second vowel box was captioned as
# the O vowel but carried 530/1840/2480, and separately proposed 570/840/2410
# for O from memory. Those two claims cannot both be right, and the second was
# flagged as unsourced. A row in that state must not reach firmware.
#
# BOTH ARE NOW SETTLED, AND THE HYPOTHESIS THE PLAN OFFERED IS CONFIRMED. In
# Peterson and Barney's own ten-vowel set, "head" and "hawed" are DIFFERENT
# vowels with different rows -- the dataset's own HEADER lists them separately,
# as `3 EH` and `7 AO`. Computed from the primary data, EH is 530/1850/2480 and
# AO is 570/840/2400. So the course PDF's caption was wrong and its numbers were
# right: they are the EH row. And the row proposed from memory for O turns out
# to be the real AO row, almost to the hertz. Nothing here now rests on that
# memory -- the shipped value was recomputed from the measurements -- but it is
# worth recording that the two sources agreed once they were correctly labelled.
#
# There is a documented mechanism for exactly this confusion, and it is worth
# recording because the next reader will find the same PDF. Praat's manual page
# for "Create formant table (Peterson & Barney 1952)" notes that the IPA
# notation in Watrous (1991) -- the transcription nearly every redistribution of
# this dataset descends from, including the CMU copy used here -- differs from
# Peterson and Barney's own for three vowels, EH, AO and ER. Where the original
# writes an open-mid symbol, Watrous writes a close-mid one. A table that
# inherits one convention and a caption that inherits the other is how an EH row
# ends up wearing an O label.
#
# THIS COPY SIDESTEPS THE WHOLE QUESTION by naming vowels in ARPABET and taking
# the numbers from the measurements rather than from any caption, so neither
# convention can bite it.
#
# --- THE ORDER IS D-29, AND THE DATA AGREES WITH IT -------------------------
#
# I -> E -> A -> O -> U, front to back, which is the vowel chart traversed once
# rather than crossfaded across. That matters here more than anywhere: EVERY
# knob position between two rows is an interpolation, so adjacency decides what
# the in-between positions sound like. Adjacent vowels interpolate into a real
# intermediate vowel; unrelated ones interpolate into a chord of two.
#
# The measurements bear the ordering out. F2 -- the front-back formant -- falls
# right down the column: 2290, 1850, 1090, 840, 880. The one step that does not
# fall is U, forty hertz above O, and that is the data being honest rather than
# a fault: /u/ is distinguished from /O/ mostly by a much lower F1 (310 against
# 570) and by lip rounding, not by F2. The last step of the morph is carried by
# F1, and it should still read as a move further back.
#
# This supersedes the A -> E -> I -> O -> U ordering that survives in older
# proposal tables. Correct it wherever it is still found.
EQ_FMT_TABLE = (
    (270.0, 2290.0, 2940.0),   # I  IY  "heed"
    (530.0, 1850.0, 2480.0),   # E  EH  "head"
    (720.0, 1090.0, 2440.0),   # A  AA  "hod"
    (570.0,  840.0, 2400.0),   # O  AO  "hawed"
    (310.0,  880.0, 2240.0),   # U  UW  "who'd"
)

# Q and gain per formant, F1 first. UNLIKE THE FREQUENCIES, THESE SIX ARE NOT
# MEASUREMENTS -- they are starting points under Claude's Discretion, gated by
# the D-17 sweep for safety and by ears for whether it sounds like a voice.
# Nothing offline in this project can judge either.
#
# The shape of both ladders is the argument, not the exact values:
#
#   F1 is the loudest and the broadest. It sits closest to where the rest of the
#   spectral energy already is, so it reads as the BODY of the vowel; a narrow
#   F1 reads as a whistle sitting on top of the music instead of as a voice
#   inside it.
#
#   F2 and F3 are narrower and quieter, in a declining ladder, which is what
#   natural vocal-tract spectral tilt does. Boosting them equally makes
#   something bright and sibilant rather than something spoken.
#
# The Qs are DELIBERATELY far lower than a real vocal tract's. A measured
# formant has a bandwidth around 40-110 Hz, which is a Q of 10 to 20; the Csound
# manual's formant table (Table III, Piche and Nix) gives exactly that range.
# Those are the numbers for SYNTHESISING a voice out of silence. This is an EQ
# boosting a signal that is already there, and a Q of 15 on a mix is nearly
# inaudible -- the boost falls between the partials. 4 / 7 / 9 is the audible
# end of the same shape.
#
# If it does not read as a vowel on hardware, RAISE THE QS FIRST -- that is the
# axis this compromise is on, and it is one edit and one sweep.
EQ_FMT_Q = (4.0, 7.0, 9.0)
EQ_FMT_G = (12.0, 9.0, 6.0)

# The shift control's two stops: exactly one octave down and one octave up, with
# a true 1.0 in the centre detent. An octave is the right span because it is the
# whole plausible range of vocal-tract lengths and then some -- it moves the
# apparent size of the mouth without leaving the territory where three bells
# still read as a voice.
EQ_FMT_DN, EQ_FMT_UP = 0.5, 2.0

# THE RANGE ASSERT, AND WHY IT IS HERE RATHER THAN IN A TEST. This is the only
# mode that writes frequencies into the object in HERTZ that did not come out of
# `gftab`. `gftab` clamps to the table's ends, so every other mode is protected
# by construction; these fifteen values, multiplied by the shift, go straight to
# 1010music's designer. EQ_F_LO is where a two-pole section's stability margin
# starts collapsing and EQ_F_HI is the designer's own SR/4 ceiling, so an
# out-of-window frequency is not a cosmetic problem.
#
# It runs at import, so it fires on EVERY build rather than when somebody
# remembers to check. Same reason as the four EQ_S_* asserts above.
assert len(EQ_FMT_TABLE) == 5 and all(len(r) == 3 for r in EQ_FMT_TABLE), \
    "EQ_FMT_TABLE must be five vowels of three formants, in D-29's I E A O U " \
    "order -- the row interpolation indexes it as a flat fifteen-float array " \
    "with a twelve-byte stride and cannot see a ragged one."
assert len(EQ_FMT_Q) == 3 and len(EQ_FMT_G) == 3, \
    "EQ_FMT_Q and EQ_FMT_G are per FORMANT, not per vowel -- three each."
assert min(min(r) for r in EQ_FMT_TABLE) * EQ_FMT_DN >= EQ_F_LO, \
    f"a formant shifted fully down reaches " \
    f"{min(min(r) for r in EQ_FMT_TABLE) * EQ_FMT_DN:.1f} Hz, below EQ_F_LO " \
    f"({EQ_F_LO}). This mode writes Hz straight into the object, so gftab's " \
    "clamp is not there to catch it and the designer sees the value directly."
assert max(max(r) for r in EQ_FMT_TABLE) * EQ_FMT_UP <= EQ_F_HI, \
    f"a formant shifted fully up reaches " \
    f"{max(max(r) for r in EQ_FMT_TABLE) * EQ_FMT_UP:.1f} Hz, above EQ_F_HI " \
    f"({EQ_F_HI}), which is the designer's own SR/4 ceiling."

EQ_NULL_FREQ = 1000.0
EQ_PHONE_HALF = 6.0           # telephone half-width, in table steps (~0.5 oct each)

# Guards -- every address above is only as good as the instruction that proves it.
EQ_VT_GUARD_VA, EQ_VT_GUARD = EQ_VT_HANDLER, EQ_HANDLER | 1
EQ_CTOR_VA,   EQ_CTOR_GUARD = 0x080ba5da, 0x46056005   # str r5,[r0] -- vtable store
EQ_DSGN_VA,   EQ_DSGN_GUARD = 0x080bae18, 0x0381eb01   # H Cut entry
EQ_COEF_VA,   EQ_COEF_GUARD = 0x080baef8, 0x6a12edc1   # vstr s13,[r1,#0x48] -- b0 slot
EQ_KONST_VA,  EQ_KONST      = 0x080baf40, 0x40c90fdb   # 2*pi -- proves freq is in Hz
EQ_GAINK_VA,  EQ_GAINK      = 0x080badcc, 0x3d4ccccd   # 0.05 -- proves gain is in dB
EQ_HAND_VA,   EQ_HAND_GUARD = 0x080bb23a, 0x46288bc1   # ldrh r1,[r0,#0x1e] / mov r0,r5

# --- no module-scope constant may be bound twice -------------------------
# The other half of BUG-01, and the half that let it happen at all. A whole
# constants block had been duplicated verbatim; Python binds top to bottom, so
# the later copy's stale values won, silently, and nothing said a word. This
# reads the file's own source and refuses any module-scope name assigned more
# than once, naming every binding's line.
#
# It lives here rather than with vprint and refuse further down because it has
# to RUN here -- after the constants are bound and before the first structure
# built out of them -- and Python will not call a function it has not read yet.
# It is stdlib only, and it skips itself without complaint if the source cannot
# be read: this script ships to users and must never fail because it could not
# find itself on disk.
#
# Only a genuinely intended re-binding belongs in REBIND_OK, and each needs its
# reason written beside it. Empty is the correct state. Adding a name is a
# decision; widening the check until it says nothing is how BUG-01 comes back.
REBIND_OK = frozenset()


def _refuse_shadowed_constants(path):
    """Stop if any module-scope name in `path` is assigned more than once.

    Walks tree.body only, so a local inside a function that happens to share a
    constant's name is not a finding. Tuple unpacking counts -- the EQ store
    offsets are written that way, and that is exactly where BUG-01 lived.
    """
    try:
        with open(path, encoding="utf-8") as f:
            tree = ast.parse(f.read(), path)
    except (OSError, TypeError, ValueError, SyntaxError):
        return                                  # cannot read itself: say nothing

    def names(node):
        if isinstance(node, ast.Name):
            yield node.id, node.lineno
        elif isinstance(node, (ast.Tuple, ast.List)):
            for elt in node.elts:
                yield from names(elt)

    bound = collections.defaultdict(list)
    for stmt in tree.body:                      # module scope only
        if isinstance(stmt, ast.Assign):
            for target in stmt.targets:
                for name, line in names(target):
                    bound[name].append(line)
        elif isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
            for name, line in names(stmt.target):
                bound[name].append(line)

    dup = {n: ls for n, ls in bound.items() if len(ls) > 1 and n not in REBIND_OK}
    assert not dup, (
        "these module-scope constants are each bound more than once, and the "
        "LAST binding is the one that wins, silently: "
        + "; ".join(f"{n} at lines " + ", ".join(str(l) for l in ls)
                    for n, ls in sorted(dup.items()))
        + ". That is exactly BUG-01's shape -- a duplicated block whose stale "
          "values overrode the live ones. Delete the duplicate, or, if the "
          "re-binding is genuinely intended, add the name to REBIND_OK above "
          "with the reason.")


_refuse_shadowed_constants(globals().get("__file__"))

# (label, hook VA, return VA, cc-value stack slot, [(cc, kind, byte offset)])
# kind 'send' -> taper(value/16383) as a float;  'bool' -> 1 byte, 0 or 1
HOOKS = [
    ("A sample/multi", 0x080b2316, 0x080b1110, 0x40, [
        # Written straight into the pad -- the setter does nothing we need.
        (CC_DELAY,   "send", 0x558),
        (CC_REVERB,  "send", 0x55c),
        (CC_REVERSE, "bool", 0x10c),   # class B has no reverse field at all

        # Everything below branches into the firmware's own case for that
        # parameter, so the clamps, lookups, conversions and recomputes are
        # the module's rather than ours. Harmless on pad types that do not use
        # a given field -- the field exists, nothing reads it.
        (CC_GDENSITY,    "jump",  CASES[CC_GDENSITY][0]),
        (CC_GSIZE,       "jump",  CASES[CC_GSIZE][0]),
        (CC_GWINDOW,     "jump",  CASES[CC_GWINDOW][0]),
        (CC_GSCATTER,    "jump",  CASES[CC_GSCATTER][0]),
        (CC_GPANRND,     "jump",  CASES[CC_GPANRND][0]),
        (CC_SUSTAIN,     "jump",  CASES[CC_SUSTAIN][0]),
        (CC_LOOPFADE,    "jump",  CASES[CC_LOOPFADE][0]),
        (CC_LOOPMODE,    "enum",  CASES[CC_LOOPMODE][0]),
        (CC_LFOWAVE,     "enum",  CASES[CC_LFOWAVE][0]),
        (CC_LFOBEATSYNC, "jbool", CASES[CC_LFOBEATSYNC][0]),
        (CC_LFOTRIG,     "jbool", CASES[CC_LFOTRIG][0]),

        # Both standard stop messages reach the same code. 120 is meant to cut
        # dead and 123 to let notes release; the module has one stop, so they
        # behave identically here rather than one of them doing nothing.
        (CC_STOP_HARD, "stop", STOP_A),
        (CC_STOP_SOFT, "stop", STOP_A),
    ]),
    ("B clip/slicer",  0x080b7cd6, 0x080b7554, 0x38, [
        (CC_DELAY,   "send", 0xb84),
        (CC_REVERB,  "send", 0xb88),

        # Sustain exists on this class too -- the earlier build hooked only
        # class A, so CC87 did nothing on Clip and Slicer pads. Its own case
        # (0x22) is two float stores and nothing else: no lookup, no clamp,
        # no recompute, so there is nothing to gain by reaching the case and
        # this class keeps its parameter switch in a different frame anyway.
        #
        (CC_SUSTAIN, "pair", (0x508, 0x918)),

        # The LFO three go through class B's own setter rather than having
        # their fields written here, because each ends in a resync that needs
        # the context object. See CLASSB_SETPARAM above.
        (CC_LFOWAVE,     "call", (P_LFOWAVE, ENUM_STEPS[CC_LFOWAVE])),
        (CC_LFOBEATSYNC, "call", (P_LFOBEATSYNC, None)),
        (CC_LFOTRIG,     "call", (P_LFOTRIG, None)),
        # PlayThru is a Slicer control and lives only in this class. Its own
        # setter is a plain boolean store, so there is nothing worth jumping
        # into -- class B keeps its parameter switch in a separate function
        # with a different frame anyway.
        (CC_PLAYTHRU, "bool", 0x255),
        (CC_STOP_HARD, "stop", STOP_B),
        (CC_STOP_SOFT, "stop", STOP_B),
    ]),
]


# Windows has no "python3" -- there the command is plain "python", and printing
# the wrong one sends a first-timer chasing a command that does not exist on
# their machine, at the exact moment they are already unsure.
PY = "python" if os.name == "nt" else "python3"


USAGE = f"""
  Community enhancements for Bitbox Micro -- 2.3.9-mod
  ----------------------------------------------------

  Adds to the stock 2.3.4 firmware:
    - MIDI control of the master compressor (with auto makeup) and EQ
    - per-pad controls the module keeps off MIDI: granular, sends, reverse,
      sustain, loop, LFO, play through, stop; and the delay's switches
    - Character: four effects in a chain on the master bus
    - a pop-up naming what a control just changed, and a CPU bar
    - Settings: Preset BPM, Clk Loss, Flip UI, OSD, Keep CCs, ProgChange

  You need your own copy of the stock 2.3.4 firmware, from 1010music's
  downloads page -- none of it is included here. It arrives as MICRO234.zip;
  unzip it and you get MICRO.BIN.

    mkdir patched
    {PY} patch_micro.py MICRO.BIN patched/MICRO.BIN

  The output must be called MICRO.BIN for the module to find it, which is why
  it goes in its own folder -- writing over your only stock copy would leave
  you no way back.

  A third argument sets the version string on the splash screen. Keep it to
  {VER_DISPLAY_MAX} characters or fewer -- the slot stores 15 but the panel shows
  fewer than that, and a truncated version means you cannot tell which image
  is on the card.

    --v238             build the previous release, 2.3.8-mod, exactly
    -v                 show the addresses and opcodes as it works
    --reassemble       verify the frozen code against its sources
    --legacy-version   allow a version string too long for the panel, for
                       rebuilding a pre-b11 image to check its fingerprint

  Not made by, endorsed by, or supported by 1010music. Read the README
  before you flash anything.
"""


# --- frozen hook code ----------------------------------------------------
# Assembler output for the hooks, with branch placeholders left zeroed
# so the bytes are origin-independent. Embedding them is what lets this script
# run with no toolchain: the assembly below is still the source of truth, and
# --reassemble rebuilds it and fails loudly if these constants have drifted.
# Regenerate by deleting an entry and running with --reassemble.

FROZEN = {
    "hookA": (
        "542b00f02f80552b00f04080562b00f051804f2b00f05880502b00f05f80512b"
        "00f06680522b00f06d80532b00f07480572b00f07b80592b00f08280582b00f0"
        "89805b2b00f08f805c2b00f095805d2b00f09b80782b00f0a1807b2b00f0a080"
        "00000000dded107ab8eee70a43f6ff7300ee903af8eee00a80ee200a00000000"
        "04f2585383ed000a00000000dded107ab8eee70a43f6ff7300ee903af8eee00a"
        "80ee200a0000000004f25c5383ed000a00000000109bb3f5005fb4bf00230123"
        "84f80c3100000000109bdb1141f67f7203fb02f39b0a109300000000109bdb11"
        "41f67f7203fb02f39b0a109300000000109bdb1141f67f7203fb02f39b0a1093"
        "00000000109bdb1141f67f7203fb02f39b0a109300000000109bdb1141f67f72"
        "03fb02f39b0a109300000000109bdb1141f67f7203fb02f39b0a109300000000"
        "109bdb1141f67f7203fb02f39b0a109300000000109b40f2030203fb02f39b0b"
        "109300000000109b40f20a0203fb02f39b0b109300000000109bb3f5005fb4bf"
        "00230123109300000000109bb3f5005fb4bf0023012310930000000000000000"
        "00000000"
        ,
        {'$t': 394, 'aA0': 100, 'aA1': 140, 'aA2': 180, 'aA3': 200, 'aA4': 220, 'aA5': 240, 'aA6': 260, 'aA7': 280, 'aA8': 300, 'aA9': 320, 'aA10': 340, 'aA11': 358, 'aA12': 376, 'aA13': 394, 'aA14': 412, 'aA15': 416, '$d': 408, '_sA': 0, 'fA': 96, 'aA0_bl': 124, 'aA0_ret': 136, 'aA1_bl': 164, 'aA1_ret': 176, 'aA2_ret': 196, 'aA3_j': 216, 'aA4_j': 236, 'aA5_j': 256, 'aA6_j': 276, 'aA7_j': 296, 'aA8_j': 316, 'aA9_j': 336, 'aA10_j': 354, 'aA11_j': 372, 'aA12_j': 390, 'aA13_j': 408, 'aA14_j': 412, 'aA15_j': 416},
    ),
    "hookB": (
        "542b00f01a80552b00f02b80572b00f03c805b2b00f04f805c2b00f060805d2b"
        "00f071805a2b00f08280782b00f089807b2b00f0888000000000dded0e7ab8ee"
        "e70a43f6ff7300ee903af8eee00a80ee200a0000000004f6843383ed000a0000"
        "0000dded0e7ab8eee70a43f6ff7300ee903af8eee00a80ee200a0000000004f6"
        "883383ed000a00000000dded0e7ab8eee70a43f6ff7300ee903af8eee00a80ee"
        "200a04f2085383ed000a04f6181383ed000a000000000e9b40f20a0203fb02f3"
        "9b0b86b040f2080203920493204649466a4600230000000006b0000000000e9b"
        "b3f5005fb4bf0023012386b040f20c0203920493204649466a46002300000000"
        "06b0000000000e9bb3f5005fb4bf0023012386b040f20d020392049320464946"
        "6a4600230000000006b0000000000e9bb3f5005fb4bf0023012384f855320000"
        "00000000000000000000"
        ,
        {'$t': 302, 'aB0': 58, 'aB1': 98, 'aB2': 138, 'aB3': 182, 'aB4': 222, 'aB5': 262, 'aB6': 302, 'aB7': 322, 'aB8': 326, '$d': 318, '_sB': 0, 'fB': 54, 'aB0_bl': 82, 'aB0_ret': 94, 'aB1_bl': 122, 'aB1_ret': 134, 'aB2_ret': 178, 'aB3_bl': 212, 'aB3_ret': 218, 'aB4_bl': 252, 'aB4_ret': 258, 'aB5_bl': 292, 'aB5_ret': 298, 'aB6_ret': 318, 'aB7_j': 322, 'aB8_j': 326},
    ),
    "hookC": (
        "30b5249c259d000000006c2c06d06d2c06d06e2c06d06f2c0bd030bd172202e0"
        "182200e0ab22b5f5005fb4bf0025012506e0ac22ed1141f67f7305fb03f5ad0a"
        "2b46002482b00094019442f67810c2f2024040f20001c0f203010000000002b0"
        "30bd"
        ,
        {'$t': 94, '$d': 90, 'cbs': 28, 'cpp': 32, 'cfl': 36, 'cwd': 50, 'cbool': 38, 'csend': 64, '_sC': 0, 'cpost': 6, 'csetp': 90},
    ),
    "hookD": ("03b4ddf888c0bcf1000f40f07f80ddf88cc0bcf1280f04d0acf12903042b2cd974e04cf67810c2f202404cf67851c2f202410268a2f11053b3f5a02f0cd24ff6a073d358b3f1c00f06d14ff6a473d35848f6a04c634503d004308842e9d355e0ddf890c0bcf5005fb4bf0023012348f6c041c0f20201535448e04cf20002c2f2064211684cf2de0cc1f2100c61450ed0c2f800c01fa002f1040102f1180c90ed000a81ed000a043004316145f7d3ddf890c000ee10cab8ee400a03eb43001aa101eb8001d1ed000a91ed011a886820ee200a30ee010a002801d0bceec00a02eb830080ed010ad2f800c053688cea030c93688cea030cd3688cea030c13698cea030c53698cea030cc2f818c003bc0000000000bf000040c100000040a005000040380000000000008002203b000020c2000000006002983a0000803f000000005542953e0000c04101000000e7a239400000f043010000004002903b000010c200000000",
        {'$t': 0, 'dout': 268, 'donoff': 34, 'dparam': 122, 'dscan': 50, 'dnextc': 88, 'dfound': 96, 'dseeded': 174, 'dstock': 276, 'dseed': 158, 'dscale': 296, 'dstore': 222, '$d': 270, '_sD': 0, 'dnext': 270}),
    "hookDr": ("03b4ddf888c0bcf1000f40f0a280ddf88cc0bcf1280f04d0acf12903052b2cd997e04cf67810c2f202404cf67851c2f202410268a2f11053b3f5a02f0cd24ff6a073d358b3f1c00f06d14ff6a473d35848f6a04c634503d004308842e9d378e0ddf890c0bcf5005fb4bf0023012348f6c041c0f2020153546be04cf20002c2f2064211684cf2de0cc1f2100c61450ed0c2f800c050a002f1040102f1180c90ed000a81ed000a043004316145f7d3052b52d0042b0bd146480168dff818c1614505d141686fea0c0c614500d141e0ddf890c01cf07f0f08bf4ceadc1c00ee10cab8ee400a03eb430040a101eb8001d1ed000a91ed011a886820ee200a30ee010a002801d0bceec00a02eb830080ed010a012b0bd82e480168dff8b8c0614505d141686fea0c0c614500d125e0d2f800c053688cea030c93688cea030cd3688cea030c13698cea030c53698cea030cc2f818c003bc00000000ddf890c01c48bcf5005f05db1b4901606fea0101416003e0002101604160ece792ed010ad2ed020ab7ee001ac1ee200a70eec10a20ee200ab4ee400af1ee10fa15d60ca0d0ed000ab4ee600af1ee10fac8bfb0ee600af1ee600ab4ee600af1ee10fab8bfb0ee600a82ed050ab2e700205061afe70000104220a606244155544f000040c100000040a005000040380000000000008002203b000020c2000000006002983a0000803f000000005542953e0000c04101000000e7a239400000f043010000004002903b000010c200000000",
        {'$t': 344, 'dout': 338, 'donoff': 34, 'dparam': 122, 'dscan': 50, 'dnextc': 88, 'dfound': 96, 'dseeded': 174, 'dstock': 472, 'dseed': 158, 'dauto': 344, 'dval': 206, 'dscale': 492, 'dstore': 264, 'dsum': 300, 'dmakeup': 376, '$d': 460, 'dauto_off': 368, 'dmk_zero': 454, 'dk36': 460, '_sD': 0, 'dnext': 340}),
    "hookE": ("4cf20002c2f2064213684cf2de0cc1f2100c634519d0c2f800c040f20003ccf24013536040f20003c4f20003936040f2a053d36043f64003136140f2000353614ff63e53c9f25013936153688cea030c93688cea030cd3688cea030c13698cea030c53698cea030c936963450ed15368c0f828349368c0f82c34d368c0f848341369c0f84c345369c0f8303400000000",
        {'$t': 0, 'everify': 74, 'eout': 140, '$d': 140, '_sE': 0, 'enext': 140}),
    "hookF": (
        "03b4ddf888c0bcf1000f40f09580ddf88cc0acf13203092b00f28e804cf24002"
        "c2f2064211684ef20e00c1f21000814226d0106040f20001516041f6c0719160"
        "41f6c071d16041f6c071116140f20001516140f20001916140f20001d16140f2"
        "0001116240f20001516240f2010191620021116351634ff6cf71c1f21001d162"
        "2499002b05d10a2001fb00f1890b51602fe0072b06d1b1f5005fb4bf00210121"
        "116226e0092b06d1b1f5005fb4bf0021012151621de0042b06d1b1f5005fb4bf"
        "00210121516114e0052b06d1b1f5005fb4bf0021012191610be0062b06d1b1f5"
        "005fb4bf00210121d16102e014a0c05c1150916a013191621068536880ea0300"
        "936880ea0300d36880ea0300136980ea0300536980ea0300936980ea0300d369"
        "80ea0300136a80ea0300536a80ea0300936a80ea0300d06203bc0000000000bf"
        "38080c10383838383838",
        {'$t': 0, 'fout': 312, 'fseeded': 128, 'fnotmode': 146, 'fbump': 242, 'fnotbyp': 164, 'fnotslope': 182, 'fnotba': 200, 'fnotbb': 218, 'fnotbc': 236, 'fplain': 236, 'fslots': 320, '$d': 314, '_sF': 0, 'fnext': 314}),
    "hookG": (
        "2de9f0412ded068b04460d464cf24006c2f2064633684ef20e00c1f210008342"
        "27d03246106040f20001516041f6c071916041f6c071d16041f6c071116140f2"
        "0001516140f20001916140f20001d16140f20001116240f20001516240f20101"
        "91620021116351634ff6cf71c1f21001d1623068736880ea0300b36880ea0300"
        "f36880ea0300336980ea0300736980ea0300b36980ea0300f36980ea0300336a"
        "80ea0300736a80ea0300b36a80ea0300f36a984240f00480b06a306300f0fcf8"
        "20462946bdec068bbde8f0410000000000b500eb800204eb820282f80c1182ed"
        "441a82ed450ac2ed460a0123c2f81c310ff6dc1353f8213001462046984700bd"
        "002000ee900af8ee600ab4eee00af1ee10fa01d5b0ee600af3ee000ab4eee00a"
        "f1ee10fa01ddb0ee600afceec00a10ee900a0f2888bf0f2000ee900af8ee600a"
        "70ee600a0ff6942101eb800191ed001ad1ed011a71eec11aa1eea01ab0ee410a"
        "704741f6c071401a48bf002000ee100ab8ee400a0ff6f011d1ed000a20ee200a"
        "f7ee000ab4eee00af1ee10fa01ddb0ee600a704700ee100ab8ee400a0ff6c011"
        "d1ed000a20ee200a704791ed000ad1ed010a01ee100ab8ee411a0ff6a012d2ed"
        "001a21ee211aa1ee200a704741f6c071421a1346002bb8bf5b42802b18d9803b"
        "00ee103ab8ee400a0ff66c11d1ed000a20ee200a002a04dc0ff65011d1ed000a"
        "03e00ff64c11d1ed000a20ee200a7047002300ee103ab8ee400a704741f6c071"
        "421a1346002bb8bf5b42802b15d9803b00ee103ab8ee400a0ff61c11d1ed000a"
        "20ee200a0ff60c11d1ed000a20ee200a002a01dcb1ee400a7047002300ee103a"
        "b8ee400a704741f6c071421a1346002bb8bf5b42802b1cd9803b00ee103ab8ee"
        "400a0ff6d401d1ed000a20ee200a002a04dc0ff6ec01d1ed000a03e00ff6e401"
        "d1ed000ab7ee001aa0ee201ab0ee410a7047b7ee000a704700b5002000eb8001"
        "04eb8101002281f80c21c1f81c2101300428f3d300227263336a002b40f0dd83"
        "7368002b00f01d80012b00f08981022b00f0fd81032b00f09380042b00f0d280"
        "052b00f0bf82062b00f08f82072b00f0e982082b00f01783092b00f05f8300f0"
        "bcbbb26841f6c073d21a10460028b8bf4042802840f2b183803800ee100ab8ee"
        "400a0ff61401d1ed000a20ee200af7ee000ab4eee00af1ee10fa01ddb0ee600a"
        "002a05dc0527f7ee000a30eec00a00e00127f3ee000a20ee200ab0ee409afff7"
        "bffeb0ee408af0680ff29471fff70dfff0ee408a00203946b0ee480af0ee680a"
        "002301ee103afff793fe01227263736a002b01d1306903e043f68070c0f20000"
        "fff7e8fef7ee000ab4eee00af1ee10fac0f26383f3ee000a30eec00a0ff28472"
        "d2ed000a20ee200a052f02d039ee400a01e039ee000afff783feb0ee408a0120"
        "3946b0ee480a0ff23c72d2ed000a002301ee103afff75cfe0322726300f03dbb"
        "00f004f80722726300f037bb00b500270ff2b86000eb870090ed008a1c20a0eb"
        "87003058002811d11020a0eb87003058fff7bcfeb0ee409a0ff29c6000eb8700"
        "d0ed008a0ff28060c15d0ee00ff2986000eb8700d0ed008a0ff2986000eb8700"
        "90ed009a0ff27c60c15d3846b0ee480af0ee680ab0ee491afff71afe0137032f"
        "c6d300bd0ff2446090ed008ab369002b0cd1f068fff78afeb0ee409a0ff23860"
        "d0ed008a0ff2206001780ae00ff23860d0ed008a0ff23c6090ed009a0ff22460"
        "01780020b0ee480af0ee680ab0ee491afff7eefd0ff2f45090ed028a7369002b"
        "0cd1b068fff762feb0ee409a0ff2e850d0ed028a0ff2d05081780ae00ff2e850"
        "d0ed028a0ff2ec5090ed029a0ff2d45081780120b0ee480af0ee680ab0ee491a"
        "fff7c6fd03227263f369002b01d1326903e041f6c072c0f2000241f6c073d21a"
        "10460028b8bf4042802840f29682803800ee100ab8ee400a0ff2dc51d1ed000a"
        "20ee200af7ee000ab4eee00af1ee10fa01ddb0ee600a002a05dc0527f7ee000a"
        "30eec00a00e00127f3ee000a20ee200afff7a6fdb0ee408a0ff28852d2ed008a"
        "02203946b0ee480af0ee680a002301ee103afff77dfd07227263736a002b00f0"
        "5c8203203946b0ee480af0ee680a002301ee103afff76cfd0f22726300f04dba"
        "30690ff21c51fff7d0fdf0ee408a7369002b01d1b06803e041f6c070c0f20000"
        "fff79ffdf3ee000a20ee200af3ee000a30eec00afff764fdb0ee408af0ee409a"
        "00200521b0ee480af0ee680a002301ee103afff73dfdb369002b01d1f06803e0"
        "41f6c070c0f20000fff77bfdf3ee000a20ee200afff744fdb0ee409ab0ee408a"
        "01200121b0ee480af0ee680a002301ee103afff71dfd03227263736a002b00f0"
        "fc81b0ee698a0ff29c42d2ed008a02200521b0ee480af0ee680a002301ee103a"
        "fff706fdb0ee498a0ff27842d2ed008a03200121b0ee480af0ee680a002301ee"
        "103afff7f5fc0f22726300f0d6b9b068fff750fdb0ee409ab369002b01d1f068"
        "03e041f6c070c0f20000fff72afd0ff26041d1ed000a20ee200a0ff25041d1ed"
        "000a70eec09a0ff22042d2ed008a39ee690afff7e5fcb0ee408a00200121b0ee"
        "480af0ee680a002301ee103afff7c0fc0ff2f432d2ed008a39ee290afff7d0fc"
        "b0ee408a01200521b0ee480af0ee680a002301ee103afff7abfc032272633069"
        "41f6c073c01a0028b8bf404280281ad9b0ee490afff7b4fcb0ee408a0ff2a832"
        "d2ed008a3069fff711fdb0ee401a02200321b0ee480af0ee680afff789fc0722"
        "726300f06ab9736a002b00f066810ff27432d2ed008a39ee690afff791fcb0ee"
        "408a02200121b0ee480af0ee680a002301ee103afff76cfc0ff24832d2ed008a"
        "39ee290afff77cfcb0ee408a03200521b0ee480af0ee680a002301ee103afff7"
        "57fc0f22726300f038b9b068fff7b2fcfff766fcb0ee408af0680ff2ec21fff7"
        "b4fcf0ee408af369002b01d1306903e043f68070c0f20000fff783fc0ff21431"
        "d1ed000a20ee209a00200321b0ee480af0ee680ab0ee491afff72afc01227263"
        "00f00bb97369002b01d1b06803e043f68070c0f20000fff7c1fcb0ee409af068"
        "fff778fcfff72cfcb0ee408a0ff29422d2ed008a00200221b0ee480af0ee680a"
        "b1ee491afff704fc01200421b0ee480af0ee680ab0ee491afff7fafb03227263"
        "00f0dbb8b068fff755fcfff709fcb0ee408ab369002b01d1f06803e043f68070"
        "c0f200000ff21821fff74ffcf0ee408af369002b01d1306903e043f68070c0f2"
        "0000fff753fcb0ee409a00200321b0ee480af0ee680ab0ee491afff7c9fb0122"
        "726300f0aab8b068fff724fcf0ee409af369002b01d1306903e043f68070c0f2"
        "00000ff2cc11fff720fcb0ee40aab369002b01d1f06803e043f68070c0f20000"
        "fff7effb0ff2ec11d1ed000a20ee209a0ff2a411d1ed008a0f227263002700ee"
        "107ab8ee400af7ee080a30ee600a20ee0a0a30ee290afff7a3fbb0ee408a3846"
        "0321b0ee480af0ee680ab0ee491afff77ffb0137042fe2d300f05fb83069fff7"
        "42fcf0ee408ab369002b01d1f06803e043f68070c0f20000fff7b3fbb0ee409a"
        "b06800ee100ab8ee400a0ff27011d1ed000a20ee200af1ee000ab4eee00af1ee"
        "10fa01ddb0ee600afceec00a10ee900a032888bf032000ee900af8ee600a30ee"
        "608a00eb40000ff2581808eb800807227263002708eb870393ed000ad3ed030a"
        "70eec00aa0ee880a20ee280a0ff2181303eb8703d3ed000a0ff2181303eb8703"
        "93ed001a21ee091a38460321fff720fb0137032fded300f000b8002707eb8701"
        "04eb8101d1f81c1100290cd1384603210ff2c40292ed000af7ee000a002301ee"
        "103afff705fb0137042fe7d300bd00bf0000000095a70b08d9a80b0899ab0b08"
        "5daf0b0819ae0b08020304000000164300002f4400007a45f4fd343f6666663f"
        "f4fd343f01030500f4fd343f6666663ff4fd343f000000000000c0c100000000"
        "f4fd343f4260e9400000803f000098413333b33e9a992940000080400000003f"
        "0000c040f4fd343f6666663f0000c0c100001041000010416f12033904028138"
        "0402813a0000803e04020139000000413333f3400000c0c100007a4404028139"
        "000000bf0000803f000080400000e0400000104100004041000010410000c040"
        "0000874300200f4500c03745008004440040e74400001b450000344400408844"
        "0080184500800e44000052440000164500009b4300005c4400000c4500002042"
        "418764422934a342b11ae942bf78264396c56d4314cea9436888f24380342d44"
        "a16377445aacb044c057fc44fb353445a1b28045bed1b7455646034600803b46"
        ,
        {'$t': 208, 'gvalid': 114, 'gout': 192, 'gapply': 696, '$d': 2768, 'gsetband': 208, 'gdtab': 2768, 'gftab': 256, 'gft1': 280, 'gft2': 298, 'gtable': 3036, 'ghalf': 354, 'ghspan': 2920, 'ghdone': 402, 'gu16': 404, 'gidxs': 2912, 'gqmap': 426, 'gccinv': 2908, 'ggain': 460, 'ggflat': 528, 'ghsinv': 2904, 'ggboost': 514, 'ggcut': 2892, 'ggend': 522, 'ggboo': 2896, 'gtiltd': 540, 'gtdflat': 602, 'gtiltg': 2900, 'gtdend': 600, 'gfshift': 614, 'gfsflat': 690, 'gfsup': 668, 'gfmtdn': 2944, 'gfsend': 676, 'gfmtup': 2948, 'gclr': 700, 'gmout': 2714, 'gmdj': 802, 'gmtwin': 1536, 'gmphone': 1774, 'gmmix': 1056, 'gmtone': 1188, 'gmtilt': 2180, 'gmnotch': 2090, 'gmpeak': 2276, 'gmmnot': 2374, 'gmfmt': 2524, 'gmdjc': 864, 'gmdjhp': 880, 'gmdjf': 882, 'gqres': 2848, 'gctl0f': 952, 'gctl0j': 960, 'gdjspr': 2916, 'gmdjs1': 1010, 'gmdjs2': 1014, 'gqfix': 2884, 'gmix3': 1068, 'gmix3l': 1072, 'gmixf': 2796, 'gmix3k': 1132, 'gmixq': 2808, 'gmixt': 2792, 'gmix3s': 1162, 'gmixkq': 2824, 'gmixkg': 2836, 'gmixk': 2820, 'gmtnk0': 1228, 'gmtns0': 1250, 'gmtnk1': 1308, 'gmtns1': 1330, 'gctl1f': 1362, 'gctl1j': 1370, 'gmtnc': 1430, 'gmtnhp': 1446, 'gmtnf': 1448, 'gctl2f': 1560, 'gctl2j': 1568, 'gctl3f': 1632, 'gctl3j': 1640, 'gctl4f': 1794, 'gctl4j': 1802, 'gbpnar': 2928, 'gbpwid': 2924, 'gqbell': 2888, 'gmbpsl': 1990, 'gqnot': 2856, 'gctl5f': 2128, 'gctl5j': 2136, 'gnotchg': 2932, 'gctl6f': 2190, 'gctl6j': 2198, 'gctl7f': 2300, 'gctl7j': 2308, 'gctl8f': 2330, 'gctl8j': 2338, 'gctl9f': 2394, 'gctl9j': 2402, 'gmnsp': 2864, 'gctl10f': 2424, 'gctl10j': 2432, 'gqmn': 2872, 'gmmnl': 2462, 'gctl11f': 2544, 'gctl11j': 2552, 'gfmtis': 2940, 'gmfmc': 2600, 'gfmtf': 2976, 'gmfmtl': 2644, 'gfmtq': 2952, 'gfmtg': 2964, 'gmfill': 2716, 'gmfnext': 2758, 'gnullf': 2936, 'gqph': 2876, '_sG': 0, 'gnext': 204}),
    "hookH": ("10b582b01a460ff2141303240094069c01940000000002b010bd0cb495f8b0220a610cbc000000000cb4354a0023136053600cbc000000000cb4314a0023136053600cbc000000000cb42d4a0023136053600cbc0000000070b50c460546000000000fb4002810d026498d420dd1217803290ad12169002901d0234900e000211f4a6fea0103536011600fbc70bd7fb5c0b0669b022b2cd1194b1c681a4dac4227d15c686fea0505ac4222d1174b1b681b78002b1dd1164b1c682378002b18d0144800000000002813d0134841996a460000000000280cd06d4640f2001614f8010b15f8011b884203d1002806d0013ef5d140b0bde87f400000000040b07fbd80c0062478250224324e4350082902240c29022478290224a428022430000c0834000c08000000004e4557204f4e4c590000bf00",
        {'hregister': 0, '$t': 252, 'hmenu': 284, 'hregister_call': 18, '$d': 256, 'hpost': 26, 'hpost_next': 36, 'hinit': 40, 'hinit_next': 52, 'hinvalidate': 56, 'hinvalidate_next': 68, 'hteardown': 72, 'hteardown_next': 84, 'hresponse': 88, 'hresponse_call': 94, 'hresponse_out': 138, 'hresponse_invalid': 126, 'hresponse_store': 128, 'hload': 142, 'hload_stock': 242, 'hload_idle': 194, 'hload_resolve': 208, 'hload_compare': 222, 'hload_same': 252, 'hload_next': 248, 'hmenu_new_ptr': 292, 'hmenu_new': 296, '_sH': 0}),
    "hookI": ("82f8143c2de90f5082b000f011f802b0bde80f500000000082f8143c2de90f5082b000f005f802b0bde80f500000000010b582b0002b0cbfc02300238df800306a46012136200000000002b010bd094a12785ab940f23f128b88a2eb03038b8040f2ef02cb88a2eb0303cb809df8083000000000681a0024",
        {'iapply_a': 0, '$t': 74, 'imadctl': 48, 'iapply_a_next': 20, '$d': 112, 'iapply_b': 24, 'iapply_b_next': 44, 'imadctl_call': 70, 'itouch': 78, 'itouch_out': 108, 'itouch_next': 112, '_sI': 0}),
    "hookJ": ("1fb582b040f23a110ff278120ff26c13022400940ff28c1401940000000002b0bde81f400000000007b500000000bde8074040f23a1100220000000040f23a1c62450bd1dff828c11bb14a4bccf800307047ccf80030ccf80430704700000000c4f83c31424b4ff0000cc3f800c0c3f804c00000000014f8081021b1dff8f8c03c4bccf800300000000094ed4f0ad4ed067a00f022f80000000090ed4f0ad0ed067a00f01af80000000094ed4f0ad4ed067a00f012f80000000090ed4f0ad0ed067a00f00af80000000094ed4f0ad4ed067a00f002f800000000dff894c0dcf80030dff890c0634517d1dff88cc0dcf80030dff880c063450fd19fed1b7af4ee477af1ee10fa08dd9fed187af4ee477af1ee10fa01dab0ee670a704787b0fbb9dff84cc0dcf80030dff848c0634519d1dff844c0dcf80030dff838c0634511d1d4ed067a9fed087af4ee477af1ee10fa08dd9fed067af4ee477af1ee10fa01da00000000000000000000a04100007a4488c00624444c4f488cc006240000000000000000436c6b204c6f73733a0000bf494e5445524e414c0000bf00484f4c440000bf00636c6b6c6f73736d6f646500",
        {'jreg': 0, '$t': 218, 'jlabel': 388, 'jvalues': 380, 'jxml': 420, 'jreg_call': 26, '$d': 352, 'jreg_next': 36, 'jstore': 40, 'jstore_call': 42, 'jstore_next': 56, 'jpost': 60, 'jpost_next': 92, 'jpost_internal': 82, 'jctor': 96, 'jctor_next': 114, 'jheld': 118, 'jheld_next': 134, 'jt1': 138, 'jhold': 218, 'jt1_next': 150, 'jt2': 154, 'jt2_next': 166, 'jt3': 170, 'jt3_next': 182, 'jt4': 186, 'jt4_next': 198, 'jt5': 202, 'jt5_next': 214, 'jhold_out': 282, 'jmin': 360, 'jmax': 364, 'jset': 284, 'jset_store': 352, 'jset_rebuild': 356, 'jinternal': 400, 'jholdtxt': 412, '_sJ': 0}),
    "hookK": ("1fb584b040f23b110ff290120ff28413022400940ff2901401940000000004b0bde81f400000000007b500000000bde8074040f23b1100220000000040f23b1c624501d000000000dff838c103b14e4bccf8003070472de9175004460000000020680168426859b38a4288bf0a4640f2e83302fb03f2b2fbf1f240f2e83c624588bf624641480168614588bf1146531a01ebe301016041688368614584bf002100238a4203d9114640f2771306e00bb1013b03e002292cbf0239002141608360bde817902de9ff512e4909682e4a914228d180462d490c684d6840f2e83c644588bf6446654588bf654664016d016426b4fbf6f4b5fbf6f5002d08bf4ff6ff7508f5907700f012f808f5967700f00ef808f59c7700f00af808f5a27700f006f8bff34f8fbde8ff5100000000386840b3b98849000022aa4209d0a2420ad2b2f5907f0ad2e02a0bd248f2e0330ae04ff6ff7307e048f6420304e04ff6004301e04ff6806302fb01f6064433807380b380f380dff81cc0ccf800600132b2f5a07fd9d3704790c00624444c4f4898c0062468ef00e030000c0834000c08435055204d657465723a00006370756d657465720000bf00",
        {'kreg': 0, '$t': 300, 'klabel_meter': 412, 'kvalues': 404, 'kxml_meter': 424, 'kreg_call1': 26, '$d': 388, 'kreg_next': 36, 'kstore': 40, 'kstore_call': 42, 'kstore_next': 56, 'kpost': 60, 'kpost_meter': 72, 'kpost_next': 68, 'kpost_store': 80, 'kstart': 86, 'kstart_call': 92, 'kdone': 192, 'kpeak_old': 174, 'kpeak_save': 188, 'kpeak_decay': 180, 'kdraw': 196, 'kdraw_out': 292, 'kbar': 300, 'kdraw_next': 296, 'kbar_ret': 386, 'kbar_loop': 310, 'kc_peak': 334, 'kc_bg': 340, 'kc_red': 346, 'kc_amber': 352, 'kc_put': 356, '_sK': 0}),
    "hookL": ("0ff2901300000000662903d1584b984208bf584870b4458900000000662903d1534b984208bf5348f0b4468900000000662a1cd130b4504c2460504c24684ff01055ac420dd34e4dac420ad242f66045655d35b14b4c9db2cbf6d015256030bc7047484c0025256030bc4ff482310000000016b52ded027b424c21680a0c4bf6d0139a4211d13d4a12684ff010539a420bd33b4b9a4208d289b207ee901af8ee677ac2ed4f7a00212160bdec027bbde8164000000000dff8c4c0ccf80040000000002de9ff512ded027b2f4909682f4a914241d12849096891423dd1274c24684ff010518c4237d325498c4234d242f66041615c81bbd4ed067a9fed1a7af4ee477af1ee10fa27dd9fed177af4ee477af1ee10fa20da9fed157a77ee877afdeee77a17ee905a282d38bf2825fa2d88bffa251048662100000000a8420cd00d4866212a46000000000e48a9b2cbf6d01101600f4801210170bdec027bbde8ff51000000000000a04100007a440000003f80bc0124285f01248cc00624a4c0062400c00724a8c0062488c00624444c4f48b4bc01245072657365742042504d3a00",
        {'lreg': 0, '$t': 328, 'llabel': 404, 'lreg_next': 4, '$d': 352, 'lgetall': 8, 'lgetall_go': 20, 'lgetall_next': 24, 'lsetall': 28, 'lsetall_go': 40, 'lsetall_next': 44, 'lpost': 48, 'lpost_next': 110, 'lpost_now': 98, 'lblock': 114, 'lblock_done': 170, 'lblock_next': 178, 'lctor': 182, 'lctor_next': 190, 'ltick': 194, 'ltick_out': 344, 'lmin': 356, 'lmax': 360, 'lhalf': 364, 'ltick_get': 310, 'ltick_set': 324, 'ltick_next': 352, '_sL': 0}),
    "hookM": ("2de9ff512ded027b80463f4c24684ff010518c4231d33d498c422ed242f66041615c4cf21025012908bf48f21f05022908bf48f2e035d4ed067a00f024f80646d4ed4f7a00f01ff8074631490968090c4bf6d012891a08f5907200f021f808f5967200f01df808f59c7200f019f808f5a27200f015f8bff34f8fbdec027bbde8ff5100000000fdeee77a17ee900a0028b8bf002040f23f139842c8bf1846704712b514684cb3b2f804c04fea4c0c002000fb0cf3234448f20002b04238bf2a461a825a829a82da8248f20002b84238bf4ff6ff729a83da831a845a84009931b9b0f59e7f03d34ff600421a829a83094a13600130b0f5a07fdad312bddff808c0ccf8004000000000a4c0062400c00724a8c0062468ef00e0",
        {'mdraw': 0, '$t': 134, 'mdraw_out': 122, 'mlen': 134, 'mbuf': 160, 'mdraw_next': 130, '$d': 260, 'mbuf_ret': 250, 'mbuf_row': 176, 'mbuf_nopend': 238, 'mctor': 252, 'mctor_next': 260, '_sM': 0}),
    "hookN": ("01b5000000002de90f50eff30080f1ee101a03b42ded100a189800000000bdec100a03bc80f30088e1ee101abde80f5001b000bd2de90f50eff30080f1ee101a03b42ded100a389839993a9a00000000bdec100a03bc80f30088e1ee101abde80f500000000000bf",
        {'nblock': 0, '$t': 80, 'nblock_orig': 2, '$d': 98, 'nblock_c': 26, 'nmidi': 52, 'nmidi_c': 76, 'nmidi_next': 98, '_sN': 0}),
    "hookO": ("2de91f5083b040f24b110ff23c120ff22c13022400940ff2381401940000000003b0bde81f50000000002de90f5040f24b11012200000000bde80f500000000040f24b1c62450ad1dff8e0c01bb10023ccf800307047364bccf800307047000000002de90f50eff30081f1ee102a06b42ded100a00000000bdec100a06bc81f30088e1ee102abde80f5000000000dff8a4c0ccf800404ff0000c000000002de90710eff3008ca3f14f000e2814d80121814047f2ff7211420ed0532b02d894f8751549b1149a1c4840f200111943416082600168013101608cf30088bde80710000000002de90710eff3008ca3f14f000e2814d80121814047f6601211420ed05a2b02d194f8541249b1129a0a4840f200211943416082600168013101608cf30088bde807100000000000bf00c306244f44534f40c3062444c3062430000c0834000c0834000c084f53443a0000bf006f736400",
        {'oreg': 0, '$t': 228, 'olabel': 328, 'ovalues': 316, 'oxml': 336, 'oreg_call': 28, '$d': 294, 'oreg_next': 38, 'ostore': 42, 'ostore_call': 52, 'ostore_next': 60, 'opost': 64, 'opost_next': 94, 'opost_off': 86, 'odraw': 98, 'odraw_c': 116, 'odraw_next': 138, 'octor': 142, 'octor_next': 154, 'opadA': 158, 'oA_out': 216, 'oA_take': 196, 'opadA_next': 224, 'opadB': 228, 'oB_out': 286, 'oB_take': 266, 'opadB_next': 294, '_sO': 0}),
    "hookOm": ("2de91f5083b040f24b110ff29c120ff27413042400940ff2981401940000000003b0bde81f50000000002de90f5040f24b11012200000000bde80f500000000040f24b1c62451bd110b5032b05d94448114601220000000001230021414813f0010f18bf0846dff800c1ccf800003f48022b38bf0846dff8f8c0ccf8000010bd000000002de90f50eff30081f1ee102a06b42ded100a00000000bdec100a06bc81f30088e1ee102abde80f5000000000dff8c0c0ccf800404ff0000c000000002de90710eff3008ca3f14f000e2819d80121814047f2ff72114213d0532b02d894f8751571b1244800682449884209d0149a234840f200111943416082600168013101608cf30088bde80710000000002de90710eff3008ca3f14f000e2819d80121814047f66012114213d05a2b02d194f8541271b1104800681049884209d0129a0f4840f200211943416082600168013101608cf30088bde807100000000080bc01244f44534f00c30624444c4f4890c0062440c3062410a406244c50455244c3062430000c08000000000000000000000000504f502d5550000043505500424f54480000bf004f53443a0000bf006f73646d6f646500",
        {'oreg': 0, '$t': 272, 'olabel': 424, 'ovalues': 388, 'oxml': 432, 'oreg_call': 28, '$d': 348, 'oreg_next': 38, 'ostore': 42, 'ostore_call': 52, 'ostore_next': 60, 'opost': 64, 'opost_next': 128, 'opost_mode': 90, 'opost_set': 84, 'odraw': 132, 'odraw_c': 150, 'odraw_next': 172, 'octor': 176, 'octor_next': 188, 'opadA': 192, 'oA_out': 260, 'oA_take': 230, 'opadA_next': 268, 'opadB': 272, 'oB_out': 340, 'oB_take': 310, 'opadB_next': 348, 'ostr_popup': 404, 'ostr_cpu': 412, 'ostr_both': 416, '_sO': 0}),
    "hookP": ("2de91f5083b040f266110ff250220ff24423022400940ff2502401940000000003b0bde81f50000000002de90f5040f26611002200000000bde80f5000000000662a06d1dff8f4c101b401208cf8000001bc40f2661c624510d110b5012b05d9774811460022000000000023dff8d4c1002003b17448ccf8000010bd0000000010b5000000000fb4eff300836d4c20686d49884202d160680130606083f300880fbc10bd0fb4eff300836648002140f22822043a8150fcd1f021c16083f300880fbc4ff0000c000000009df87830072b01d0000000002de91f10eff3008c26980f2813d827994f390e290fd8289a554b0401241a641803eb4404a4f8402003eb4004208c01228a40104320848cf30088bde81f10000000002de9ff50eff3008c4df804cd87b0474c0020206120684649884279d16068a168884204d0a0600021e1606161a161e568f02d6dd2a0690130a0613e4f4af61060385c002831d04af204203e58a6f11050b0f5a02f29d24df6d84030584df6dc417158884221d149f23c703858a8b948f2d430385848f2d831795888420dd148f6e010385848f6e4117958884205d160690130606120283bd30ae000206061a06940f2ee21884233d302e0002060612fe042f6dc50c0f201003058102828d81027f02d24d20f20b5fbf0f101fb105204eb41031b8cd340db0717d007200090002001900590029112f14f00039004eb4500b0f8400004901048206130466946000000000135013fdbd101e00135d8e7e56007b05df804cb8cf30088bde8ff500000000000bfb4bc012480bc012400a406245045454b782902244c50455230000c0834000c084b656570204343733a0000bf6b65657063637300",
        {'preg': 0, '$t': 538, 'plabel': 604, 'pvalues': 596, 'pxml': 616, 'preg_call': 28, '$d': 566, 'preg_next': 38, 'pstore': 42, 'pstore_call': 52, 'pstore_next': 60, 'pedit': 64, 'ppost': 82, 'ppost_next': 124, 'ppost_mode': 108, 'ppost_set': 102, 'ppost_store': 118, 'pfinal': 128, 'pfinal_call': 130, 'pfinal_out': 156, 'pctor': 164, 'pctor_clr': 178, 'pctor_next': 198, 'pmidi': 202, 'pmidi_cc': 214, 'pmidi_stock': 210, 'pmidi_out': 268, 'pmidi_next': 276, 'pblock': 280, 'pb_out': 552, 'pb_have': 326, 'pb_unsteady': 450, 'pb_busy': 434, 'pb_ready': 456, 'pb_loop': 472, 'pb_save': 550, 'pb_next': 546, 'pb_post': 534, 'pblock_next': 566, '_sP': 0}),
}

# Reduced C objects (see "C hooks" below): compressed, origin-independent, and
# linked at build time with no toolchain.
#
# FROZEN_C["chr"] is compiled from character/character.c. Its effects are ports
# of Airwindows plugins by Chris Johnson (airwindows), used under the MIT
# licence, whose notice travels with them:
#
#   MIT License
#
#   Copyright (c) 2018 Chris Johnson
#
#   Permission is hereby granted, free of charge, to any person obtaining a
#   copy of this software and associated documentation files (the
#   "Software"), to deal in the Software without restriction, including
#   without limitation the rights to use, copy, modify, merge, publish,
#   distribute, sublicense, and/or sell copies of the Software, and to
#   permit persons to whom the Software is furnished to do so, subject to
#   the following conditions:
#
#   The above copyright notice and this permission notice shall be included
#   in all copies or substantial portions of the Software.
#
#   THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS
#   OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
#   MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT.
#   IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY
#   CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT,
#   TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE
#   SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
FROZEN_C = {
    "chr": (
        "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"
        "+e8v/ge5JAlb"
    ),
}

REASSEMBLE = False
VERBOSE = False
LEGACY_VERSION = False    # --legacy-version: lift the splash DISPLAY limit only,
                          # for rebuilding a pre-b11 image to check its hash

# What the patch gives you, in the order it gets applied. Kept here so the
# summary the user reads is generated from the same constants that do the work
# and cannot drift into a comfortable lie.
FEATURES = [
    ("Compressor on/off, ch 1", f"CC {CC_COMP_ONOFF}"),
    ("Compressor threshold, ch 1", f"CC {CC_COMP_FIRST}"),
    ("Compressor ratio, ch 1", f"CC {CC_COMP_FIRST + 1}"),
    ("Compressor attack, ch 1", f"CC {CC_COMP_FIRST + 2}"),
    ("Compressor release, ch 1", f"CC {CC_COMP_FIRST + 3}"),
    ("Compressor makeup, ch 1", f"CC {CC_COMP_FIRST + 4}"),
    ("EQ mode select, ch 1", f"CC {CC_EQ_MODE}"),
    ("EQ control A, ch 1", f"CC {CC_EQ_A}"),
    ("EQ control B, ch 1", f"CC {CC_EQ_B}"),
    ("EQ control C, ch 1", f"CC {CC_EQ_C}"),
    ("EQ button A, ch 1", f"CC {CC_EQ_BTN_A}"),
    ("EQ button B, ch 1", f"CC {CC_EQ_BTN_B}"),
    ("EQ button C, ch 1", f"CC {CC_EQ_BTN_C}"),
    ("EQ master bypass, ch 1", f"CC {CC_EQ_BYPASS}"),
    ("EQ filter slope, ch 1", f"CC {CC_EQ_SLOPE}"),
    ("Granular density, per pad", f"CC {CC_GDENSITY}"),
    ("Granular grain size, per pad", f"CC {CC_GSIZE}"),
    ("Granular window, per pad", f"CC {CC_GWINDOW}"),
    ("Granular scatter, per pad", f"CC {CC_GSCATTER}"),
    ("Granular pan random, per pad", f"CC {CC_GPANRND}"),
    ("Delay send, per pad", f"CC {CC_DELAY}"),
    ("Reverb send, per pad", f"CC {CC_REVERB}"),
    ("Reverse, per pad", f"CC {CC_REVERSE}"),
    ("Envelope sustain, per pad", f"CC {CC_SUSTAIN}"),
    ("Loop mode, per pad", f"CC {CC_LOOPMODE}"),
    ("Loop crossfade, per pad", f"CC {CC_LOOPFADE}"),
    ("Play through, slicer pads", f"CC {CC_PLAYTHRU}"),
    ("LFO wave, per pad", f"CC {CC_LFOWAVE}"),
    ("LFO beat-sync, per pad", f"CC {CC_LFOBEATSYNC}"),
    ("LFO retrigger, per pad", f"CC {CC_LFOTRIG}"),
    ("Delay beat-sync, global", f"CC {CC_BEATSYNC}"),
    ("Delay ping-pong, global", f"CC {CC_PINGPONG}"),
    ("Delay filter, global", f"CC {CC_DLYFILTER}"),
    ("Delay filter width, global", f"CC {CC_DLYWIDTH}"),
    ("Stop this pad", f"CC {CC_STOP_HARD} / {CC_STOP_SOFT}"),
]

# The three additions that live on the module's Settings screen, not on a CC.
SETTINGS_FEATURES = [
    ("ProgChange: OFF/ON/NEW ONLY", "TOOLS > Settings"),
    ("Clk Loss: INTERNAL/HOLD", "TOOLS > Settings"),
    ("Flip UI, screen and touch", "TOOLS > Settings"),
]


def vprint(msg):
    """Addresses, opcodes and offsets. Only when -v asks for them."""
    if VERBOSE:
        print(f"       {msg}")


def refuse(headline, *detail):
    """Stop, and explain it in a way somebody can act on."""
    sys.stdout.flush()          # else the buffered header lands after this
    out = [f"\n  X  {headline}\n"]
    out += [f"     {line}".rstrip() for line in detail]
    out.append("\n     Nothing has been written. Your firmware file is untouched.\n")
    sys.stderr.write("\n".join(out) + "\n")
    raise SystemExit(1)


def asm(src):
    with tempfile.TemporaryDirectory() as td:
        s, obj = os.path.join(td, "p.s"), os.path.join(td, "p.o")
        open(s, "w").write(src)
        r = subprocess.run(["xcrun", "clang", "-target", "thumbv7em-none-eabi",
                            "-mcpu=cortex-m7", "-mfloat-abi=hard", "-c", "-o", obj, s],
                           capture_output=True, text=True)
        if r.returncode:
            sys.stderr.write(src + "\n" + r.stderr)
            raise SystemExit("assembly failed")
        return parse_elf32(open(obj, "rb").read())


def parse_elf32(e):
    shoff, = struct.unpack_from("<I", e, 0x20)
    shent, shnum, shstrndx = struct.unpack_from("<HHH", e, 0x2E)
    sh = lambda i: dict(zip(
        ("name", "type", "flags", "addr", "off", "size", "link", "info", "align", "entsz"),
        struct.unpack_from("<10I", e, shoff + i * shent)))
    secs = [sh(i) for i in range(shnum)]
    shstr = secs[shstrndx]
    nm = lambda o: e[shstr["off"] + o:e.index(b"\0", shstr["off"] + o)].decode()
    ti = next(i for i, s in enumerate(secs) if nm(s["name"]) == ".text")
    code = e[secs[ti]["off"]:secs[ti]["off"] + secs[ti]["size"]]
    syms = {}
    for s in secs:
        if s["type"] != 2:
            continue
        st = secs[s["link"]]
        for i in range(s["size"] // s["entsz"]):
            o = s["off"] + i * s["entsz"]
            n, val, _, _, _, shndx = struct.unpack_from("<IIIBBH", e, o)
            if shndx == ti and n:
                syms[e[st["off"] + n:e.index(b"\0", st["off"] + n)].decode()] = val & ~1
    return code, syms


def enc_b_bl(src, dst, is_bl):
    off = dst - (src + 4)
    assert -(1 << 24) <= off < (1 << 24), f"out of range {off}"
    imm = off >> 1
    S, I1, I2 = (imm >> 23) & 1, (imm >> 22) & 1, (imm >> 21) & 1
    hw1 = 0xF000 | (S << 10) | ((imm >> 11) & 0x3FF)
    hw2 = ((0xD000 if is_bl else 0x9000) | ((((~I1) & 1) ^ S) << 13)
           | ((((~I2) & 1) ^ S) << 11) | (imm & 0x7FF))
    return struct.pack("<HH", hw1, hw2)


def enc_bne_w(src, dst):
    off = dst - (src + 4)
    assert -(1 << 20) <= off < (1 << 20), f"out of range {off}"
    imm = off >> 1
    hw1 = 0xF000 | (((imm >> 19) & 1) << 10) | (1 << 6) | ((imm >> 11) & 0x3F)
    hw2 = 0x8000 | (((imm >> 17) & 1) << 13) | (((imm >> 18) & 1) << 11) | (imm & 0x7FF)
    return struct.pack("<HH", hw1, hw2)


def assemble(key, src):
    """Machine code for one hook: frozen bytes by default, clang on request.

    The frozen blobs are this file's own assembler output with every branch
    placeholder still zeroed -- callers patch those per origin, so the bytes
    do not depend on where the block lands and can safely be a constant.
    That is what lets the patcher run with no toolchain installed.

    Pass --reassemble (needs a thumbv7em assembler) to rebuild from the source
    below and prove the constants still match it.
    """
    if REASSEMBLE:
        code, syms = asm(src)
        if key not in FROZEN:                      # generating a new blob
            print(f'    "{key}": ("{code.hex()}",\n'
                  f'        {syms!r}),')
            return code, syms
        if (code.hex(), syms) != FROZEN[key]:
            sys.exit(f"FROZEN[{key!r}] is STALE -- the assembler now produces\n"
                     f'    "{key}": ("{code.hex()}",\n        {syms!r}),')
        print(f"  reassembled {key:8} matches frozen blob ({len(code)} bytes)")
        return code, syms

    if key not in FROZEN:
        sys.exit(f"no frozen blob for {key!r}; rerun with --reassemble")
    blob, syms = FROZEN[key]
    return bytes.fromhex(blob), syms


# --- C hooks: compiled by clang, linked HERE, frozen ---------------------------
# Every hook above is hand-written assembly. The Character chain (hook N) is
# DSP, which is written in C. There is no ELF linker on a stock Mac, so this
# file does the link itself: clang produces one relocatable object, the object
# is reduced to its allocated sections, its relocations and its exported
# symbols, and `c_link` places it at whatever origin the image has reached.
#
# The REDUCED OBJECT is what gets frozen (FROZEN_C), not linked bytes, because
# the origin moves with the opt-in hooks in front of it. Linking a frozen
# object needs no toolchain and is deterministic, so a no-toolchain build still
# reproduces the image byte for byte. --reassemble recompiles the C and refuses
# if the frozen object has drifted from the source.
#
# Rules the C must keep, enforced here rather than remembered:
#   - no writable data at all (.data/.bss): state lives at fixed patch-RAM
#     addresses, reached through pointers, never in a linked section;
#   - no libc: every external symbol must be passed in `externs` explicitly;
#   - only the relocation types below. Anything else refuses loudly, because a
#     relocation applied wrongly is a wild branch, not a build error.
C_FLAGS = ("--target=thumbv7em-none-eabihf", "-mcpu=cortex-m7", "-mfpu=fpv5-d16",
           "-mfloat-abi=hard", "-mthumb", "-O2", "-ffreestanding", "-fno-builtin",
           "-fno-exceptions", "-fno-unwind-tables", "-fno-asynchronous-unwind-tables",
           "-fno-math-errno", "-ffp-contract=off", "-ffunction-sections",
           "-fdata-sections", "-fno-common", "-fno-stack-protector", "-mno-unaligned-access",
           "-Wall", "-Werror=implicit-function-declaration", "-Wdouble-promotion",
           "-Werror=double-promotion")
R_ARM_NONE, R_ARM_ABS32, R_ARM_REL32 = 0, 2, 3
R_ARM_THM_CALL, R_ARM_THM_JUMP24 = 10, 30
R_ARM_THM_MOVW_ABS_NC, R_ARM_THM_MOVT_ABS = 47, 48
C_RELOCS = {R_ARM_NONE, R_ARM_ABS32, R_ARM_REL32, R_ARM_THM_CALL,
            R_ARM_THM_JUMP24, R_ARM_THM_MOVW_ABS_NC, R_ARM_THM_MOVT_ABS}
# Section-name prefixes that are linked; everything else allocated must be empty.
C_KEEP = (".text", ".rodata")
C_IGNORE = (".ARM.exidx", ".ARM.extab", ".comment", ".note", ".ARM.attributes",
            ".llvm_addrsig", ".group", ".debug", ".strtab", ".symtab", ".shstrtab",
            ".rel.", ".rela.")


def c_compile(path):
    """clang -> relocatable ELF bytes. Only ever called under --reassemble."""
    with tempfile.TemporaryDirectory() as td:
        obj = os.path.join(td, "c.o")
        r = subprocess.run(["xcrun", "clang", *C_FLAGS, "-c", "-o", obj, path],
                           capture_output=True, text=True)
        if r.returncode:
            sys.stderr.write(r.stderr)
            raise SystemExit(f"C compile failed: {path}")
        return open(obj, "rb").read()


def c_reduce(e):
    """Reduce a relocatable ELF32 ARM object to what linking needs.

    Returns (sections, exports): sections = [(name, align, hex data,
    [(offset, type, ref)])] in link order (all .text first, then .rodata);
    ref = ("sec", index, value) or ("ext", name). exports = {name: (index,
    value)} for global symbols, value keeping the Thumb bit of a function."""
    if e[:4] != b"\x7fELF" or e[4] != 1 or e[5] != 1 or struct.unpack_from("<H", e, 18)[0] != 40:
        refuse("The C object is not a little-endian 32-bit ARM ELF.")
    shoff, = struct.unpack_from("<I", e, 0x20)
    shent, shnum, shstrndx = struct.unpack_from("<HHH", e, 0x2E)
    sh = [dict(zip(("name", "type", "flags", "addr", "off", "size", "link", "info",
                    "align", "entsz"), struct.unpack_from("<10I", e, shoff + i * shent)))
          for i in range(shnum)]
    strs = lambda sec, o: e[sh[sec]["off"] + o:e.index(b"\0", sh[sec]["off"] + o)].decode()
    names = [strs(shstrndx, s["name"]) for s in sh]
    SHF_WRITE, SHF_ALLOC, SHF_EXEC = 1, 2, 4
    keep = []
    for i, (s, n) in enumerate(zip(sh, names)):
        if not (s["flags"] & SHF_ALLOC) or s["size"] == 0:
            continue
        if n.startswith(C_IGNORE):
            continue
        if s["flags"] & SHF_WRITE or s["type"] != 1 or not n.startswith(C_KEEP):
            refuse(f"The C object has a section this linker will not place: {n}",
                   "Hook C must hold no writable data and no unknown sections.")
        keep.append(i)
    keep.sort(key=lambda i: (0 if names[i].startswith(".text") else 1, i))
    index = {i: k for k, i in enumerate(keep)}
    symtab = next(i for i, s in enumerate(sh) if s["type"] == 2)
    syms = []
    for j in range(sh[symtab]["size"] // 16):
        n, val, size, info, other, shndx = struct.unpack_from(
            "<IIIBBH", e, sh[symtab]["off"] + 16 * j)
        syms.append((strs(sh[symtab]["link"], n) if n else "", val, info, shndx))
    exports = {}
    for n, val, info, shndx in syms:
        if n and (info >> 4) == 1 and shndx in index:          # STB_GLOBAL
            exports[n] = (index[shndx], val)
    sections = []
    for i in keep:
        relocs = []
        for r, s in enumerate(sh):
            if s["type"] == 9 and s["info"] == i:              # SHT_REL for this section
                for k in range(s["size"] // 8):
                    off, info = struct.unpack_from("<II", e, s["off"] + 8 * k)
                    typ, sym = info & 0xff, info >> 8
                    if typ not in C_RELOCS:
                        refuse(f"Unknown relocation type {typ} in {names[i]}+{off:#x}.",
                               "Teach c_link how to apply it before using it.")
                    if typ == R_ARM_NONE:
                        continue
                    n, val, sinfo, shndx = syms[sym]
                    if shndx == 0:
                        ref = ("ext", n)
                    elif shndx in index:
                        ref = ("sec", index[shndx], 0 if (sinfo & 0xf) == 3 else val)
                    else:
                        refuse(f"Relocation in {names[i]} refers to {names[shndx]}, "
                               "which is not linked.")
                    relocs.append((off, typ, ref))
            elif s["type"] == 4 and s["info"] == i and s["size"]:
                refuse("RELA relocations are not supported here.")
        s = sh[i]
        sections.append((names[i], max(s["align"], 1),
                         e[s["off"]:s["off"] + s["size"]].hex(), sorted(relocs)))
    return sections, dict(sorted(exports.items()))


def _thm_branch_addend(hw1, hw2):
    s = (hw1 >> 10) & 1
    i1 = 1 - (((hw2 >> 13) & 1) ^ s)
    i2 = 1 - (((hw2 >> 11) & 1) ^ s)
    imm = (s << 24) | (i1 << 23) | (i2 << 22) | ((hw1 & 0x3ff) << 12) | ((hw2 & 0x7ff) << 1)
    return imm - (1 << 25) if s else imm


def _thm_imm16(hw1, hw2):
    return (((hw1 & 0xf) << 12) | (((hw1 >> 10) & 1) << 11)
            | (((hw2 >> 12) & 7) << 8) | (hw2 & 0xff))


def _thm_put_imm16(hw1, hw2, v):
    hw1 = (hw1 & ~0x040f) | ((v >> 12) & 0xf) | (((v >> 11) & 1) << 10)
    hw2 = (hw2 & ~0x70ff) | (((v >> 8) & 7) << 12) | (v & 0xff)
    return hw1, hw2


def c_link(obj, org, externs=None):
    """Place a reduced object at `org` and apply every relocation.

    Returns (code, syms): syms are offsets from `org` with the Thumb bit
    cleared, the same convention as the assembled hooks."""
    externs = externs or {}
    sections, exports = obj
    assert org % 4 == 0
    out, base = bytearray(), []
    for name, align, data, _ in sections:
        while (org + len(out)) % align:
            out += b"\0"
        base.append(org + len(out))
        out += bytes.fromhex(data)
    for k, (name, _, _, relocs) in enumerate(sections):
        for off, typ, ref in relocs:
            P = base[k] + off
            o = P - org
            if ref[0] == "sec":
                S = base[ref[1]] + ref[2]
            elif ref[1] in externs:
                S = externs[ref[1]]
            else:
                refuse(f"The C hook calls {ref[1]!r}, which nothing provides.",
                       "Hook C links against no libc. Implement it, or pass it",
                       "as an extern with its firmware address.")
            if typ in (R_ARM_THM_CALL, R_ARM_THM_JUMP24):
                hw1, hw2 = struct.unpack_from("<HH", out, o)
                is_bl = typ == R_ARM_THM_CALL
                if hw1 & 0xf800 != 0xf000 or hw2 & 0xd000 != (0xd000 if is_bl else 0x9000):
                    refuse(f"Relocation type {typ} at {name}+{off:#x} is not on a "
                           "bl/b.w (a blx to ARM code, perhaps). Refusing.")
                A = _thm_branch_addend(hw1, hw2)
                if ref[0] == "ext" and not S & 1:
                    refuse(f"Extern {ref[1]!r} is not a Thumb address (bit 0 clear).")
                out[o:o + 4] = enc_b_bl(P, (S & ~1) + A + 4, is_bl)
            elif typ in (R_ARM_THM_MOVW_ABS_NC, R_ARM_THM_MOVT_ABS):
                hw1, hw2 = struct.unpack_from("<HH", out, o)
                A = _thm_imm16(hw1, hw2)
                A -= (A & 0x8000) << 1
                X = (S + A) & 0xffffffff
                v = X & 0xffff if typ == R_ARM_THM_MOVW_ABS_NC else X >> 16
                struct.pack_into("<HH", out, o, *_thm_put_imm16(hw1, hw2, v))
            elif typ == R_ARM_ABS32:
                A, = struct.unpack_from("<I", out, o)
                struct.pack_into("<I", out, o, (S + A) & 0xffffffff)
            elif typ == R_ARM_REL32:
                A, = struct.unpack_from("<I", out, o)
                struct.pack_into("<I", out, o, (S + A - P) & 0xffffffff)
            else:                                   # c_reduce already refused these
                refuse(f"Unhandled relocation type {typ}.")
    while len(out) % 4:
        out += b"\0"
    syms = {n: (base[k] + v - org) & ~1 for n, (k, v) in exports.items()}
    return bytes(out), syms


def c_freeze_text(obj):
    """Canonical, compressed text form of a reduced object, for FROZEN_C."""
    import base64, zlib
    raw = base64.b64encode(zlib.compress(repr(obj).encode(), 9)).decode()
    return tuple(raw[i:i + 76] for i in range(0, len(raw), 76))


def c_thaw(lines):
    import base64, zlib
    return ast.literal_eval(zlib.decompress(base64.b64decode("".join(lines))).decode())


def c_object(key, path):
    """The reduced object for one C hook: frozen by default, clang on request."""
    if REASSEMBLE:
        obj = c_reduce(c_compile(path))
        if key not in FROZEN_C:
            print(f'    "{key}": (')
            for line in c_freeze_text(obj):
                print(f'        "{line}"')
            print("    ),")
            return obj
        if c_thaw(FROZEN_C[key]) != obj:
            print(f'FROZEN_C[{key!r}] is STALE -- the compiler now produces:')
            for line in c_freeze_text(obj):
                print(f'        "{line}"')
            sys.exit(f"FROZEN_C[{key!r}] is STALE")
        size = sum(len(s[2]) // 2 for s in obj[0])
        print(f"  recompiled {key:8} matches frozen object ({size} bytes)")
        return obj
    if key not in FROZEN_C:
        sys.exit(f"no frozen C object for {key!r}; rerun with --reassemble")
    return c_thaw(FROZEN_C[key])


def build(org, ret, vslot, arms, tag):
    def send_arm(sym, off):
        return f"""
    vldr    s15, [sp, #{vslot:#x}]
    vcvt.f32.s32 s0, s15
    movw    r3, #{CC_MAX}
    vmov    s1, r3
    vcvt.f32.s32 s1, s1
    vdiv.f32 s0, s0, s1
    .global {sym}_bl
{sym}_bl:
    .short 0,0
    addw    r3, r4, #{off:#x}
    vstr    s0, [r3]
    .global {sym}_ret
{sym}_ret:
    .short 0,0
"""
    def jump_arm(sym, _off):
        """Rescale the value in place, then fall into the firmware's own case.

        r2/r3 are free here: every target case loads what it needs before
        reading either, and r0/r1/s16-s21 are untouched.
        """
        return f"""
    ldr     r3, [sp, #{vslot:#x}]
    asrs    r3, r3, #7                 /* 14-bit widened -> 0..127   */
    movw    r2, #{CC7_TO_1000_MUL}
    mul     r3, r3, r2
    lsrs    r3, r3, #{CC7_TO_1000_SHIFT}  /* -> 0..1000, exact at both ends */
    str     r3, [sp, #{vslot:#x}]
    .global {sym}_j
{sym}_j:
    .short 0,0
"""
    def enum_arm(sym, target):
        """Even CC bands onto N discrete values, then into the firmware's case.

        band = (raw * N) >> 14. The value arrives as (7-bit << 7), so this is
        (cc7 * N) >> 7 with one multiply instead of two shifts, and it lands
        exactly on 0 and N-1 at the extremes for every N we use.
        """
        n = ENUM_STEPS[target_cc[sym]]
        return f"""
    ldr     r3, [sp, #{vslot:#x}]
    movw    r2, #{n}
    mul     r3, r3, r2
    lsrs    r3, r3, #14                /* -> 0..{n - 1}, even bands */
    str     r3, [sp, #{vslot:#x}]
    .global {sym}_j
{sym}_j:
    .short 0,0
"""
    def jbool_arm(sym, _target):
        """Zero or one into the value slot, then into the firmware's case.

        These cases test the slot against zero themselves, so the arm only has
        to make it definitely zero or definitely not.
        """
        return f"""
    ldr     r3, [sp, #{vslot:#x}]
    cmp.w   r3, #{BOOL_THRESHOLD}
    ite     lt
    movlt   r3, #0
    movge   r3, #1
    str     r3, [sp, #{vslot:#x}]
    .global {sym}_j
{sym}_j:
    .short 0,0
"""
    def stop_arm(sym, _target):
        """Branch straight into the pad's own stop code.

        Neither class tests the message key here, and we are already inside the
        target pad's handler, so this stops exactly that pad. Nothing to
        convert and nothing to set up.
        """
        return f"""
    .global {sym}_j
{sym}_j:
    .short 0,0
"""
    def bool_arm(sym, off):
        return f"""
    ldr     r3, [sp, #{vslot:#x}]
    cmp.w   r3, #{BOOL_THRESHOLD}
    ite     lt
    movlt   r3, #0
    movge   r3, #1
    strb.w  r3, [r4, #{off:#x}]
    .global {sym}_ret
{sym}_ret:
    .short 0,0
"""
    def pair_arm(sym, offs):
        """One float into BOTH copies the Clip/Slicer voice keeps.

        Class B stores every parameter twice, in two engine structures 0x410
        bytes apart, and its own setter writes both every time. Writing one
        would leave the two halves disagreeing.

        No taper: the parameter is stored as value/1000.0 and a CC spans
        0..1000 of it, so the float wanted is just cc/16383 -- which is what
        the send arm computes before its taper call.
        """
        lo, hi = offs
        return f"""
    vldr    s15, [sp, #{vslot:#x}]
    vcvt.f32.s32 s0, s15
    movw    r3, #{CC_MAX}
    vmov    s1, r3
    vcvt.f32.s32 s1, s1
    vdiv.f32 s0, s0, s1
    addw    r3, r4, #{lo:#x}
    vstr    s0, [r3]
    addw    r3, r4, #{hi:#x}
    vstr    s0, [r3]
    .global {sym}_ret
{sym}_ret:
    .short 0,0
"""
    def call_arm(sym, spec):
        """Hand the value to class B's own setter instead of writing fields.

        spec is (parameter id, number of enum steps or None for a boolean).
        The value is read BEFORE sp moves, then a small message frame is built
        with the id at +0xc and the value at +0x10 -- the only two fields the
        setter reads. r4 is the voice and r9 the context object it wants, both
        still live from the prologue. sp is restored before the arm returns,
        because the code it returns to addresses its frame off sp.
        """
        pid, steps = spec
        if steps is None:
            conv = f"""
    cmp.w   r3, #{BOOL_THRESHOLD}
    ite     lt
    movlt   r3, #0
    movge   r3, #1"""
        else:
            conv = f"""
    movw    r2, #{steps}
    mul     r3, r3, r2
    lsrs    r3, r3, #14                /* -> 0..{steps - 1}, even bands */"""
        return f"""
    ldr     r3, [sp, #{vslot:#x}]{conv}
    sub     sp, #{CLASSB_MSG:#x}
    movw    r2, #{pid}
    str     r2, [sp, #0xc]
    str     r3, [sp, #0x10]
    mov     r0, r4
    mov     r1, r9
    mov     r2, sp
    movs    r3, #0
    .global {sym}_bl
{sym}_bl:
    .short 0,0
    add     sp, #{CLASSB_MSG:#x}
    .global {sym}_ret
{sym}_ret:
    .short 0,0
"""
    dispatch, bodies, fixups = [], [], [(f"f{tag}", ret, False)]
    target_cc = {}
    for n, (cc, kind, off) in enumerate(arms):
        sym = f"a{tag}{n}"
        dispatch.append(f"    cmp   r3, #{cc}\n    beq.w {sym}")
        if kind == "send":
            bodies.append(f"{sym}:{send_arm(sym, off)}")
            fixups += [(f"{sym}_bl", TAPER, True), (f"{sym}_ret", ret, False)]
        elif kind == "pair":
            bodies.append(f"{sym}:{pair_arm(sym, off)}")
            fixups += [(f"{sym}_ret", ret, False)]
        elif kind == "call":
            bodies.append(f"{sym}:{call_arm(sym, off)}")
            fixups += [(f"{sym}_bl", CLASSB_SETPARAM, True),
                       (f"{sym}_ret", ret, False)]
        elif kind in ("jump", "enum", "jbool", "stop"):
            # off is a case address inside this same function, not a field
            # offset -- the arm branches there instead of returning.
            gen = {"jump": jump_arm, "enum": enum_arm,
                   "jbool": jbool_arm, "stop": stop_arm}[kind]
            target_cc[sym] = cc
            bodies.append(f"{sym}:{gen(sym, off)}")
            fixups += [(f"{sym}_j", off, False)]
        else:
            bodies.append(f"{sym}:{bool_arm(sym, off)}")
            fixups += [(f"{sym}_ret", ret, False)]

    src = f"""
    .syntax unified
    .thumb
    .section .text
    .global _s{tag}
    .thumb_func
_s{tag}:
{chr(10).join(dispatch)}
    .global f{tag}
f{tag}:
    .short 0,0
{"".join(bodies)}
"""
    code, syms = assemble(f"hook{tag}", src)
    code = bytearray(code)
    for sym, dst, isbl in fixups:
        o = syms[sym]
        code[o:o + 4] = enc_b_bl(org + o, dst, isbl)
    return bytes(code)


def build_midi_hook(org):
    """Replacement for the CC-event post: do the original post, then translate
    our two CC numbers into set-parameter messages aimed at the delay object."""
    src = f"""
    .syntax unified
    .thumb
    .section .text
    .global _sC
    .thumb_func
_sC:
    push    {{r4, r5, lr}}
    ldr     r4, [sp, #{MSG_CC_SP + 12:#x}]      /* CC number  */
    ldr     r5, [sp, #{MSG_VAL_SP + 12:#x}]     /* 14-bit value */
    .global cpost
cpost:
    .short 0,0                                  /* bl FUN_080950c0 (original) */
    cmp     r4, #{CC_BEATSYNC}
    beq     cbs
    cmp     r4, #{CC_PINGPONG}
    beq     cpp
    cmp     r4, #{CC_DLYFILTER}
    beq     cfl
    cmp     r4, #{CC_DLYWIDTH}
    beq     cwd
    pop     {{r4, r5, pc}}
cbs:
    movs    r2, #{P_BEATSYNC}
    b       cbool
cpp:
    movs    r2, #{P_PINGPONG}
    b       cbool
cfl:
    movs    r2, #{P_DLYFILTER}
cbool:
    cmp.w   r5, #{BOOL_THRESHOLD}
    ite     lt
    movlt   r5, #0
    movge   r5, #1
    b       csend
cwd:
    /* Width is continuous, so it wants 0..1000 rather than a flag. */
    movs    r2, #{P_DLYWIDTH}
    asrs    r5, r5, #7
    movw    r3, #{CC7_TO_1000_MUL}
    mul     r5, r5, r3
    lsrs    r5, r5, #{CC7_TO_1000_SHIFT}
csend:
    mov     r3, r5
    movs    r4, #0
    sub     sp, #8
    str     r4, [sp, #0]
    str     r4, [sp, #4]
    movw    r0, #{MANAGER & 0xffff}
    movt    r0, #{MANAGER >> 16}
    movw    r1, #{DELAY_KEY & 0xffff}
    movt    r1, #{DELAY_KEY >> 16}
    .global csetp
csetp:
    .short 0,0                                  /* bl cmd39_SetParam */
    add     sp, #8
    pop     {{r4, r5, pc}}
"""
    code, syms = assemble("hookC", src)
    code = bytearray(code)
    code[syms["cpost"]:syms["cpost"] + 4] = enc_b_bl(org + syms["cpost"], MIDI_POST_FN, True)
    code[syms["csetp"]:syms["csetp"] + 4] = enc_b_bl(org + syms["csetp"], SETPARAM_FN, True)
    return bytes(code)


def build_comp_hook(org, hookc, full_range=False):
    """CC 40-45, chained in front of hook C.

    full_range (rc1, --v239 only; frozen as hookDr): CC 41-45 reach the top of
    their range. The dispatcher delivers every CC as byte << 7 (0x080439de:
    `ldrb r0, [r4, #2]; lsls r0, #7` -- it never pairs CC n with n+32), so
    127 arrived as 16256 and the top was 99.2% of the way: threshold -0.3 dB,
    ratio 19.85:1, makeup +35.4 dB. A value whose low seven bits are all zero
    is widened to (v << 7) | v, so 127 -> 16383. A value with any low bit set
    would be true 14-bit and passes unchanged -- no path in 2.3.4 produces
    one, but the rule cannot break one. CC 40 (a switch) is unchanged.

    CC 40 writes the session's on/off flag directly -- the session is allocated
    once at boot, so that target is stable, and hardware testing confirms it.
    It is found by proof rather than arithmetic: scan the engine's pointer slots
    and validate each candidate by reading back the safety limiter's two timing
    constants, which nothing on the module ever writes.

    CC 41-45 do not touch the engine at all. They write the patch's own store,
    which hook E applies to the live object every block.
    """
    CCMAX = CC_MAX
    dstock = "\n".join(f"    .word  {w:#010x}   /* {n} */" for w, n in DEFAULTS)
    rng = "".join(
        f"    .float {(hi - lo) / CCMAX!r}\n    .float {lo!r}\n    .word  {1 if i else 0}\n"
        for lo, hi, i in COMP_RANGES)
    widen = """
    tst     r12, #0x7f                 /* 7-bit (byte << 7): fill the range */
    it      eq
    orreq.w r12, r12, r12, lsr #7""" if full_range else ""
    # rc1 (release only, with full_range): CC 46, auto makeup. See AUTO_ below.
    last = CC_COMP_COUNT - 1 + (1 if full_range else 0)
    auto_on = lambda no: f"""
    ldr     r0, =0x{AUTO_RAM:08x}
    ldr     r1, [r0]
    ldr     r12, =0x{AUTO_MAGIC:08x}
    cmp     r1, r12
    bne     {no}
    ldr     r1, [r0, #4]
    mvn     r12, r12
    cmp     r1, r12
    bne     {no}"""
    pre_val = f"""
    cmp     r3, #5
    beq     dauto                      /* CC 46 */
    cmp     r3, #4
    bne     dval{auto_on("dval")}
    b       dout                       /* CC 45 while auto is on: ignored outright */
dval:""" if full_range else ""
    post_store = f"""
    cmp     r3, #1
    bhi     dsum{auto_on("dsum")}
    b       dmakeup                    /* threshold or ratio moved: recompute */
dsum:""" if full_range else ""
    auto_code = f"""
dauto:                                 /* r2 = the store, seeded */
    ldr     r12, [sp, #{MSG_VAL_SP + 8:#x}]
    ldr     r0, =0x{AUTO_RAM:08x}
    cmp.w   r12, #{BOOL_THRESHOLD}
    blt     dauto_off
    ldr     r1, =0x{AUTO_MAGIC:08x}
    str     r1, [r0]
    mvn     r1, r1
    str     r1, [r0, #4]
    b       dmakeup                    /* the jump happens on enable */
dauto_off:
    movs    r1, #0
    str     r1, [r0]
    str     r1, [r0, #4]
    b       dout                       /* makeup stays where auto left it */
dmakeup:                               /* makeup = -threshold x (1 - 1/ratio) */
    vldr    s0, [r2, #4]
    vldr    s1, [r2, #8]
    vmov.f32 s2, #1.0
    vdiv.f32 s1, s2, s1
    vsub.f32 s1, s1, s2
    vmul.f32 s0, s0, s1
    vcmp.f32 s0, s0
    vmrs    APSR_nzcv, fpscr
    bvs     dmk_zero                   /* a broken ratio: no makeup, never -36 */
    adr     r0, dk36
    vldr    s1, [r0]
    vcmp.f32 s0, s1
    vmrs    APSR_nzcv, fpscr
    it      gt
    vmovgt.f32 s0, s1
    vneg.f32 s1, s1
    vcmp.f32 s0, s1
    vmrs    APSR_nzcv, fpscr
    it      lt
    vmovlt.f32 s0, s1
    vstr    s0, [r2, #20]
    b       dsum
dmk_zero:
    movs    r0, #0
    str     r0, [r2, #20]
    b       dsum
    .balign 4
dk36:
    .float  36.0
    .ltorg""" if full_range else ""
    src = f"""
    .syntax unified
    .thumb
    .section .text
    .global _sD
    .thumb_func
_sD:
    /* r0 AND r1 ARE THE STOCK CALL'S ARGUMENTS. DO NOT CLOBBER THEM.
       This hook is reached by a chain that BRANCHES rather than calls, so it
       shares the register state of the original `bl FUN_080950c0` at
       MIDI_POST_CALL, 0x08093f14. The two instructions immediately before that
       call -- `ldr r0,[r7]` and `mov r1,r6` -- are setting up arguments, and
       0x080950c0 is not a function at all, it is a this-adjusting thunk: it
       adds 0x127dc to the incoming r0 and tail-branches to the queue-append at
       0x080a396c. r2, r3 and r12 are scratch here; r0 and r1 must arrive
       intact.

       Hook D used to fall out of `dout` with r0 holding a patch-store address
       around 0x2406c000, on BOTH its CC 40 path and its CC 41-45 path.
       0x2406c000 + 0x127dc = 0x2407e7dc, which IS mapped -- AXI SRAM -- so it
       corrupted quietly instead of faulting: up to 64 records of 24 bytes once
       per boot, with the queue's own `cmp r7, #0x3f` bound stopping it there.
       Hook F's equivalent formed 0x101227aa, which is NOT mapped, and hard
       faulted on every EQ CC until it was bracketed. The same defect and
       different luck, and the luck is the whole reason this one shipped in two
       firmware versions while hook F's was found in an afternoon. Fixed under
       D-32 in 2.3.7-b16.

       The pop sits at `dout`, BEFORE `dnext`. Hook C is downstream of that
       tail branch and reads the caller frame at sp+0x90 and sp+0x94 counting
       only its own `push {{r4, r5, lr}}`, so a pop placed after the branch
       would leave SP eight bytes low and break the delay FX CCs -- a different
       fault, and not one a compressor test would catch. Every caller-frame
       read below is therefore written `+ 8`, and THERE ARE FOUR OF THEM, NOT
       THREE: MSG_VAL_SP is read twice, once at `dfound` for the CC 40
       threshold compare and once at `dseeded` for the CC 41-45 conversion. */
    push    {{r0, r1}}
    ldr     r12, [sp, #{MSG_CHAN_SP + 8:#x}]   /* +8: our own push */
    cmp     r12, #{COMP_CHANNEL}
    bne.w   dout
    ldr     r12, [sp, #{MSG_CC_SP + 8:#x}]
    cmp     r12, #{CC_COMP_ONOFF}
    beq     donoff
    sub.w   r3, r12, #{CC_COMP_FIRST}
    cmp     r3, #{last}
    bls     dparam
    b       dout
donoff:
    movw    r0, #{SCAN_LO & 0xffff}
    movt    r0, #{SCAN_LO >> 16}
    movw    r1, #{SCAN_HI & 0xffff}
    movt    r1, #{SCAN_HI >> 16}
dscan:
    ldr     r2, [r0]
    sub.w   r3, r2, #{RAM_LO:#x}
    cmp.w   r3, #{RAM_SPAN:#x}
    bhs     dnextc
    movw    r3, #{PROOF_A_OFF:#x}
    ldr     r3, [r2, r3]
    cmp.w   r3, #{PROOF_A_VAL}
    bne     dnextc
    movw    r3, #{PROOF_R_OFF:#x}
    ldr     r3, [r2, r3]
    movw    r12, #{PROOF_R_VAL}
    cmp     r3, r12
    beq     dfound
dnextc:
    adds    r0, #4
    cmp     r0, r1
    blo     dscan
    b       dout
dfound:
    ldr     r12, [sp, #{MSG_VAL_SP + 8:#x}]
    cmp.w   r12, #{BOOL_THRESHOLD}
    ite     lt
    movlt   r3, #0
    movge   r3, #1
    movw    r1, #{COMP_ONOFF_OFF & 0xffff}
    movt    r1, #{COMP_ONOFF_OFF >> 16}
    strb    r3, [r2, r1]
    b       dout
dparam:
    movw    r2, #{PATCH_RAM & 0xffff}
    movt    r2, #{PATCH_RAM >> 16}
    ldr     r1, [r2]
    movw    r12, #{PATCH_MAGIC & 0xffff}
    movt    r12, #{PATCH_MAGIC >> 16}
    cmp     r1, r12
    beq     dseeded
    str     r12, [r2]                  /* first touch: seed from stock */
    adr     r0, dstock
    add     r1, r2, #4
    add.w   r12, r2, #{4 + 4 * CC_COMP_COUNT}
dseed:
    vldr    s0, [r0]
    vstr    s0, [r1]
    adds    r0, #4
    adds    r1, #4
    cmp     r1, r12
    blo     dseed
dseeded:{pre_val}
    ldr     r12, [sp, #{MSG_VAL_SP + 8:#x}]{widen}
    vmov    s0, r12
    vcvt.f32.u32 s0, s0
    add.w   r0, r3, r3, lsl #1
    adr     r1, dscale
    add.w   r1, r1, r0, lsl #2
    vldr    s1, [r1]
    vldr    s2, [r1, #4]
    ldr     r0, [r1, #8]
    vmul.f32 s0, s0, s1
    vadd.f32 s0, s0, s2
    cmp     r0, #0
    beq     dstore
    vcvt.u32.f32 s0, s0
dstore:
    add.w   r0, r2, r3, lsl #2
    vstr    s0, [r0, #4]
{post_store}
    ldr     r12, [r2]                  /* checksum = magic XOR all five */
    ldr     r3, [r2, #4]
    eor.w   r12, r12, r3
    ldr     r3, [r2, #8]
    eor.w   r12, r12, r3
    ldr     r3, [r2, #12]
    eor.w   r12, r12, r3
    ldr     r3, [r2, #16]
    eor.w   r12, r12, r3
    ldr     r3, [r2, #20]
    eor.w   r12, r12, r3
    str     r12, [r2, #{PATCH_SUM}]
dout:
    pop     {{r0, r1}}                  /* before dnext -- see the note at _sD */
    .global dnext
dnext:
    .short 0,0{auto_code}
    .align 2
dstock:
{dstock}
dscale:
{rng}"""
    code, syms = assemble("hookDr" if full_range else "hookD", src)
    code = bytearray(code)
    code[syms["dnext"]:syms["dnext"] + 4] = enc_b_bl(org + syms["dnext"], hookc, False)
    return bytes(code)


def build_comp_dsp_hook(org):
    """Hook E -- the one that makes the values stick.

    The engine calls the compressor's processing function once per audio block
    with the live object in r0. Stamping our store onto it there means the patch
    owns those five parameters continuously: whatever the engine does to the
    object, the next block puts them back. A magic word gates the whole thing,
    so a clobbered store switches the feature off rather than writing rubbish
    into the audio path.

    r0 and r1 belong to the real DSP and are untouched; r2, r3 and ip are
    caller-saved and free.
    """
    # First audio block after power-on: lay down the defaults so the module
    # comes up as a working compressor rather than 1010music's headroom trim.
    # Costs nothing after that -- the magic word means this runs exactly once.
    _lines = ["    str     r12, [r2]                  /* claim the store */"]
    for _i, (_w, _n) in enumerate(DEFAULTS):
        _lines.append(f"    movw    r3, #{_w & 0xffff:#06x}")
        if _w >> 16:
            _lines.append(f"    movt    r3, #{_w >> 16:#06x}")
        _lines.append(f"    str     r3, [r2, #{4 + _i * 4}]              /* {_n} */")
    _lines.append(f"    movw    r3, #{DEFAULT_SUM & 0xffff:#06x}")
    _lines.append(f"    movt    r3, #{DEFAULT_SUM >> 16:#06x}")
    _lines.append(f"    str     r3, [r2, #{PATCH_SUM}]             /* checksum */")
    eseed = "\n".join(_lines)

    src = f"""
    .syntax unified
    .thumb
    .section .text
    .global _sE
    .thumb_func
_sE:
    movw    r2, #{PATCH_RAM & 0xffff}
    movt    r2, #{PATCH_RAM >> 16}
    ldr     r3, [r2]
    movw    r12, #{PATCH_MAGIC & 0xffff}
    movt    r12, #{PATCH_MAGIC >> 16}
    cmp     r3, r12
    beq     everify
{eseed}
everify:
    ldr     r3, [r2, #4]               /* verify the whole store, not just */
    eor.w   r12, r12, r3               /* the magic -- a half-eaten store  */
    ldr     r3, [r2, #8]               /* must switch the feature OFF, not */
    eor.w   r12, r12, r3               /* stamp rubbish into the audio.    */
    ldr     r3, [r2, #12]
    eor.w   r12, r12, r3
    ldr     r3, [r2, #16]
    eor.w   r12, r12, r3
    ldr     r3, [r2, #20]
    eor.w   r12, r12, r3
    ldr     r3, [r2, #{PATCH_SUM}]
    cmp     r3, r12
    bne     eout
    ldr     r3, [r2, #4]
    str.w   r3, [r0, #{COMP_F_THRESH:#x}]
    ldr     r3, [r2, #8]
    str.w   r3, [r0, #{COMP_F_RATIO:#x}]
    ldr     r3, [r2, #12]
    str.w   r3, [r0, #{COMP_F_ATTACK:#x}]
    ldr     r3, [r2, #16]
    str.w   r3, [r0, #{COMP_F_RELEASE:#x}]
    ldr     r3, [r2, #20]
    str.w   r3, [r0, #{COMP_F_MAKEUP:#x}]
eout:
    .global enext
enext:
    .short 0,0
"""
    code, syms = assemble("hookE", src)
    code = bytearray(code)
    code[syms["enext"]:syms["enext"] + 4] = enc_b_bl(org + syms["enext"], COMP_DSP_FN, False)
    return bytes(code)


def _eq_seed(reg, scratch):
    """Lay the store down with the module's boot state: DJ Filter, knobs centred.

    Both hooks can be first to run -- hook G on the first audio block, hook F on
    the first CC -- so both must be able to seed, and they must agree exactly or
    the checksum will reject whichever went second. `gen` starts at 1 and `agen`
    at 0, so hook G applies the mode on its very first block.

    `scratch` IS DESTROYED, and naming it is not a style preference. This block
    is eleven stores long and every one of them needs a register to store FROM;
    it used to take that register (`r3`) silently. Hook G can afford that -- it
    reloads everything from `r6` afterwards -- but hook F is holding the CC
    index in `r3` at exactly this point, and the block left it holding the
    checksum constant `0x1010ffce` instead. Every compare at `fseeded` then
    missed and `fplain` did `ldrb r0, [r0, r3]`, reading `fslots + 0x1010ffce`
    -- about `0x181f44d6`, reserved space on this part. Bus fault, hard fault,
    watchdog. That was the reboot on the first EQ CC after boot, and only the
    first, because the seed runs once and the module never got far enough to
    have a second.

    So the caller passes a register it can prove is dead here, and the assert
    below refuses the one mistake that is easy to make twice.

    All three controls are seeded at EQ_CENTRE, and that is D-16's whole
    mechanism. The old seed laid the character control down at 0, so Notch
    booted at its widest Q and Band Pass at its narrowest width -- both of the
    things D-16 says must not happen, measured by the simulator's --boot-state
    before this changed. Centring costs no new constant. Every button and the
    self test boot released, because a module that powers on with a control
    already overridden is a module that looks broken.
    """
    boot = {EQ_S_MODE: EQ_BOOT_MODE,
            EQ_S_GA: EQ_CENTRE, EQ_S_GB: EQ_CENTRE, EQ_S_GC: EQ_CENTRE,
            EQ_S_BTN_A: 0, EQ_S_BTN_B: 0, EQ_S_BTN_C: 0,
            EQ_S_BYP: 0, EQ_S_SLOPE: 0, EQ_S_GEN: 1}
    chk = EQ_MAGIC
    for o in EQ_S_LIVE:
        chk ^= boot[o]
    assert scratch not in (reg, "r2"), (
        f"the seed's scratch register ({scratch}) is also carrying the magic "
        f"({reg}) or the store base (r2), so the block would eat its own inputs")
    out = [f"    str     {reg}, [r2]                    /* claim the store */"]
    for off in sorted(boot):
        v = boot[off]
        out.append(f"    movw    {scratch}, #{v & 0xffff:#06x}")
        if v >> 16:
            out.append(f"    movt    {scratch}, #{v >> 16:#06x}")
        out.append(f"    str     {scratch}, [r2, #{off}]")
    out.append(f"    movs    {scratch}, #0")
    out.append(f"    str     {scratch}, [r2, #{EQ_S_AGEN}]     /* != gen, so we apply */")
    out.append(f"    str     {scratch}, [r2, #{EQ_S_MASK}]")
    out.append(f"    movw    {scratch}, #{chk & 0xffff:#06x}")
    out.append(f"    movt    {scratch}, #{chk >> 16:#06x}")
    out.append(f"    str     {scratch}, [r2, #{EQ_S_SUM}]")
    return "\n".join(out)


def _eq_checksum(dst, base, scratch):
    """magic XOR the ten live words, into `dst`."""
    out = [f"    ldr     {dst}, [{base}]"]
    for off in EQ_S_LIVE:
        out.append(f"    ldr     {scratch}, [{base}, #{off}]")
        out.append(f"    eor.w   {dst}, {dst}, {scratch}")
    return "\n".join(out)


def _f(sreg, label, areg):
    """Load a float constant. adr.w reaches +/-4095; a vldr literal only +/-1020,
    and this hook is far longer than that."""
    return f"    adr.w   {areg}, {label}\n    vldr    {sreg}, [{areg}]"


def build_eq_midi_hook(org, hookd):
    """Hook F -- CC 50-58, chained in FRONT of hook D.

    Chained rather than folded into hook D so hooks A-E stay byte-identical and
    the compressor remains provably untouched by this feature.

    It never touches the EQ object (R3). Every parameter write and every call
    into 1010music's filter designers happens on the audio thread in hook G,
    because that is the thread their smoother and their designers already run
    on. All this does is record the knob and bump a generation counter.
    """
    # The slots table, indexed by CC - CC_EQ_FIRST. Only the three continuous
    # controls ever reach `fplain`; mode, the three buttons, master bypass and
    # the self test are all special-cased above it, so their entries here are
    # unreachable. They point at EQ_S_SCRATCH -- a dead word outside EQ_S_LIVE,
    # so a write there changes nothing and is not checksummed.
    #
    # An entry of 0 would point at the store's MAGIC word, and a future edit
    # that dropped one of those special cases would then silently re-seed the
    # store on every CC: the EQ would forget its settings at apparently random
    # moments, with nothing on screen to say why. That is a smaller version of
    # the mistake this whole phase exists to fix, and EQ_S_SCRATCH costs four
    # bytes of RAM to make it harmless.
    slots = [EQ_S_SCRATCH] * (CC_EQ_LAST - CC_EQ_FIRST + 1)
    for cc, off in ((CC_EQ_A, EQ_S_GA), (CC_EQ_B, EQ_S_GB), (CC_EQ_C, EQ_S_GC)):
        slots[cc - CC_EQ_FIRST] = off
    tbl = "".join(f"    .byte {v}\n" for v in slots)

    def fbin(cc, off, nxt, what):
        """One binary CC: at or above half scale is engaged (D-03).

        Same three-instruction idiom for all six, and every one of them leaves
        by `b fbump` -- the shared tail that bumps `gen`. Since hook G applies
        the store on every block, skipping `fbump` no longer makes a control
        appear dead; but `gen` is the store's own record of how many CCs it has
        accepted, and a write that did not bump it would make that record lie.
        Every accepted CC leaves by the same tail, with no exceptions.
        """
        return f"""    cmp     r3, #{cc - CC_EQ_FIRST}
    bne     {nxt}
    cmp.w   r1, #{BOOL_THRESHOLD}      /* {what} */
    ite     lt
    movlt   r1, #0
    movge   r1, #1
    str     r1, [r2, #{off}]
    b       fbump
{nxt}:
"""

    src = f"""
    .syntax unified
    .thumb
    .section .text
    .global _sF
    .thumb_func
_sF:
    /* r0 AND r1 ARE THE STOCK CALL'S ARGUMENTS. DO NOT CLOBBER THEM.
       This hook replaces `bl FUN_080950c0` at MIDI_POST_CALL, and the two
       instructions immediately before that call are `ldr r0,[r7]` and
       `mov r1,r6` -- they are setting up arguments. FUN_080950c0 is not a
       function, it is a this-adjusting thunk:
           mov r3,r0 / ldr r0,=0x000127d8 / mov.w r2,#0x400 / add r0,r3
           b.w 0x080a83a4   ->  r2==0x400 -> add r0,#4 -> b.w 0x080a396c
       so the object pointer it finally dereferences is `incoming r0 + 0x127dc`.
       r2 and r3 are dead on entry (the thunk overwrites both), r12 is scratch
       by AAPCS -- but r0 and r1 must arrive intact.

       Hook F used to fall out of `fbump` with r0 holding the freshly computed
       EQ CHECKSUM, about 0x1010ffce. 0x1010ffce + 0x127dc = 0x101227aa, which
       is not mapped on this part: bus fault, hard fault, watchdog reboot. That
       is the reboot on every EQ CC, and it is why moving the store (dbgZ) did
       not help -- the fatal value is the checksum itself, not where it lives.

       BOTH HOOKS AT THIS SITE NOW BRACKET. Hook D committed the identical
       violation and shipped with it from 2.3.6: its leftover r0 was a store
       address around 0x2406c000, so 0x2406c000 + 0x127dc = 0x2407e7dc landed
       in mapped AXI SRAM and merely corrupted rather than faulting. Same
       defect, different luck -- and the luck is why hook F's was found in an
       afternoon and hook D's took two firmware versions. Fixed under D-32 in
       2.3.7-b16, with this same bracket. ANY future hook chained at
       MIDI_POST_CALL must do the same. See DEV-README's chain-ABI note before
       adding one. */
    push    {{r0, r1}}
    ldr     r12, [sp, #{MSG_CHAN_SP + 8:#x}]   /* +8: our own push */
    cmp     r12, #{EQ_CHANNEL}
    bne.w   fout
    ldr     r12, [sp, #{MSG_CC_SP + 8:#x}]
    sub.w   r3, r12, #{CC_EQ_FIRST}
    cmp     r3, #{CC_EQ_LAST - CC_EQ_FIRST}
    bhi.w   fout
    movw    r2, #{EQ_STORE & 0xffff}
    movt    r2, #{EQ_STORE >> 16}
    ldr     r1, [r2]
    movw    r0, #{EQ_MAGIC & 0xffff}
    movt    r0, #{EQ_MAGIC >> 16}
    cmp     r1, r0
    beq     fseeded
{_eq_seed("r0", "r1")}
fseeded:
    ldr     r1, [sp, #{MSG_VAL_SP + 8:#x}]
    cmp     r3, #{CC_EQ_MODE - CC_EQ_FIRST}
    bne     fnotmode
    /* mode = (7-bit value * 10) >> 7, which is (raw * 10) >> 14 in the raw
       domain. Ten slots of ~12.8 CC values each, and the controller sends ten
       steps over Min 6 / Max 122, so every step sits six CC values clear of a
       window boundary -- the widest margin this design has had, because fewer
       modes means wider windows. r0 held the magic for the compare above and
       is dead here. */
    movs    r0, #{EQ_M_SLOTS}
    mul     r1, r1, r0
    lsrs    r1, r1, #14
    str     r1, [r2, #{EQ_S_MODE}]
    b       fbump
fnotmode:
{fbin(CC_EQ_BYPASS, EQ_S_BYP, "fnotbyp", "master bypass, encoder 1's push")}\
{fbin(CC_EQ_SLOPE, EQ_S_SLOPE, "fnotslope", "filter slope, 12 or 24 dB/oct")}\
{fbin(CC_EQ_BTN_A, EQ_S_BTN_A, "fnotba", "button A")}\
{fbin(CC_EQ_BTN_B, EQ_S_BTN_B, "fnotbb", "button B")}\
{fbin(CC_EQ_BTN_C, EQ_S_BTN_C, "fnotbc", "button C")}\
fplain:
    adr     r0, fslots
    ldrb    r0, [r0, r3]
    str     r1, [r2, r0]
fbump:
    ldr     r1, [r2, #{EQ_S_GEN}]
    adds    r1, #1
    str     r1, [r2, #{EQ_S_GEN}]
{_eq_checksum("r0", "r2", "r3")}
    str     r0, [r2, #{EQ_S_SUM}]
fout:
    pop     {{r0, r1}}                  /* hand the thunk its arguments back */
    .global fnext
fnext:
    .short 0,0
    .align 2
fslots:
{tbl}"""
    code, syms = assemble("hookF", src)
    code = bytearray(code)
    code[syms["fnext"]:syms["fnext"] + 4] = enc_b_bl(org + syms["fnext"], hookd, False)
    return bytes(code)


def build_eq_dsp_hook(org):
    """Hook G -- installed in the EQ class's vtable slot +0x0c.

    The node already runs once per block, already asks the message for its audio
    handle and frame count, and then never asks for the buffers. This supplies
    the arithmetic that is missing, then tail-branches into the stock handler so
    the parameter path, the coefficient smoother and the chain forward all keep
    working untouched.

    It runs BEFORE the stock handler because the stock handler is what forwards
    the block on -- so it filters with the coefficients the smoother produced last
    block. 128 frames, about 2.7 ms. Every exit is pass-through, which for this
    node is exactly stock behaviour.
    """
    zero = "    movs    r3, #0\n    vmov    s2, r3"

    # Formant's vowel table, emitted ROW MAJOR with one vowel per line, so the
    # twelve-byte stride `gmfmt` indexes with is visible in the source that
    # emits it rather than only in the routine that walks it. The names are the
    # dataset's own ARPABET codes and Peterson and Barney's keywords -- see
    # EQ_FMT_TABLE for the citation and for why the file avoids IPA.
    fmt_rows = ("I  IY  heed", "E  EH  head", "A  AA  hod",
                "O  AO  hawed", "U  UW  who'd")
    fmt_tbl = "\n".join(
        "    .float " + ", ".join(f"{v!r}" for v in row)
        + " " * max(1, 34 - len("    .float " + ", ".join(f"{v!r}" for v in row)))
        + f"/* {name} */"
        for row, name in zip(EQ_FMT_TABLE, fmt_rows))

    def setband(band, type_, freq="s16", q="s17", gain=None):
        g = zero.replace("s2", "s2") if gain is None else gain
        return (f"    movs    r0, #{band}\n    movs    r1, #{type_}\n"
                f"    vmov.f32 s0, {freq}\n    vmov.f32 s1, {q}\n{g}\n    bl      gsetband\n")

    # ---- D-03, the button semantic, written here because this is where a
    # future session will come looking for it rather than re-deriving it from
    # a mode routine ---------------------------------------------------------
    #
    #   FORCED, NOT TOGGLED.   Engaged means that control is held at one fixed
    #     value. There is no second press, no third state and no mode cycle.
    #   RELEASED HANDS BACK.   Released means the encoder takes over again, on
    #     the very next audio block, because hook F bumped `gen`.
    #   THE STORED VALUE IS NEVER OVERWRITTEN.   `ctl()` reads the control word
    #     and the button word and writes neither, so releasing returns the sound
    #     to exactly where the knob is. That is what stops a knob-jump.
    #   THE THRESHOLD IS HALF SCALE.   Hook F converts the CC to 0 or 1 against
    #     BOOL_THRESHOLD, which is what makes a momentary footswitch and a
    #     latching toggle behave identically. Nothing here re-decides that.
    #
    # The split is structural, not conventional: storage is hook F on the MIDI
    # thread, interpretation is here on the audio thread, where 1010music's
    # designers and their coefficient smoother already run (R2/R3). `ctl` is
    # defined inside build_eq_dsp_hook and so cannot be called from
    # build_eq_midi_hook even by accident.
    _ctl_n = [0]

    def ctl(slot, btn=None, forced=None, dest="r0", scratch="r3"):
        """Emit: load control `slot` into `dest`, or the literal `forced` if
        `btn` is engaged.

        slot   - a store offset, EQ_S_GA / EQ_S_GB / EQ_S_GC
        btn    - a store offset, EQ_S_BTN_A / EQ_S_BTN_B / EQ_S_BTN_C, or None
                 for no button
        forced - the raw CC-domain value substituted when the button is engaged,
                 normally one of EQ_FORCE_CENTRE / EQ_FORCE_MAX / EQ_FORCE_MIN

        With `btn=None` this emits the plain `ldr` and nothing else, byte for
        byte what a mode with no button on that control emits today -- so
        adopting it across an existing routine is provably a no-op.

        THERE ARE EXACTLY TWO KINDS OF BUTTON, and a mode uses one or the other
        for a given band, never both and never a third shape:

        1. The FORCED-VALUE kind, which is this function. The band is still
           designed; one of its inputs is pinned. DJ Filter's button A (force
           control A to centre, which lands in the detent and is a real
           bypass), Dual Cut's A and B (force a cut fully open), Band Pass's B
           (force the width widest), Notch's depth slam, and the flat/neutral
           and maximum-effect buttons the four unbuilt modes are specified to
           get.

        2. The KILL kind, which does NOT use `ctl` at all. It still calls
           `gsetband` for that band and it still sets that band's mask bit --
           what changes is the band's TYPE, swapped to a kill shape out of
           `gmixk` / `gmixkq` / `gmixkg`. Kill low becomes a high-pass at
           150 Hz, kill high a low-pass at 4 kHz, and kill mid a wide -24 dB
           scoop at 700 Hz. Mixer EQ's three kill buttons are this kind, and
           Tone + Filter's two will be when it is built.

           DO NOT "kill" a band by disabling it. b6 did exactly that, reasoning
           that a disabled band passes nothing. That is true of the BAND and
           false of the AUDIO: a disabled band applies no filter, so the signal
           passes straight through untouched. Worse, Rule 1 already puts every
           band at 0 dB at centre, which is where the knobs rest, so the kill
           was removing something that was already transparent and did nothing
           audible at all. The user found it in the first minute of play.
           Attenuating instead would not have worked either -- `ggain`'s floor
           is -24 dB and a shelf at -24 dB still passes everything past its
           corner.

           D-25 is what makes the type swap the only available lever, and it is
           worth stating plainly: NO BAND IS EVER SWITCHED OFF. A band a mode
           does not want is designed as a flat Param, H(z) = 1 exactly. Only
           the target ever changes, so 1010music's 50 ms smoother ramps every
           transition in both directions, which is what removed the b7
           reconfiguration ring and the b8 click. So the only lever any button
           has is WHAT gets designed, never WHETHER.

        DJ Filter's button B is a third shape only in appearance -- it *adds*
        the cascaded second section rather than swapping a type -- and it is
        the one already in the file, at the bottom of `gmdj`.
        """
        if btn is None:
            return f"    ldr     {dest}, [r6, #{slot}]\n"
        if forced is None:
            raise ValueError("ctl() with a button needs a forced value -- a "
                             "button that forces nothing is not a button")
        if not 0 <= forced <= EQ_CC_MAX:
            raise ValueError(f"ctl() forced value {forced} is outside the raw "
                             f"CC domain 0..{EQ_CC_MAX}; a value the encoder "
                             f"cannot reach is a coefficient set no sweep has "
                             f"covered")
        if dest == scratch:
            raise ValueError(f"ctl() would load the button flag into {dest} "
                             f"and then overwrite it with the control value")
        n = _ctl_n[0]
        _ctl_n[0] += 1
        # A unique suffix per call, so one routine can call this more than once
        # without a label collision. `setband` needs no such thing because it
        # emits no labels; this is the one structural difference between them.
        return f"""    ldr     {scratch}, [r6, #{btn}]    /* D-03: read the button, write nothing */
    cmp     {scratch}, #0
    bne     gctl{n}f                   /* engaged -> the forced value */
    ldr     {dest}, [r6, #{slot}]      /* released -> the encoder's own value,
                                          which no button ever overwrote */
    b       gctl{n}j
gctl{n}f:
    movw    {dest}, #{forced & 0xffff}
    movt    {dest}, #{forced >> 16}
gctl{n}j:
"""

    src = f"""
    .syntax unified
    .thumb
    .section .text
    .global _sG
    .thumb_func
_sG:
    /* r8 is pushed only to keep SP 8-byte aligned across the `blx` into
       1010music's designer: five core registers plus lr is 24 bytes, which is
       what AAPCS wants, where the natural {{r4,r5,r6,r7,lr}} would leave SP at
       4. `vpush {{d8,d9,d10}}` is NOT optional -- the mode routines park
       frequency, Q and gain in s16..s21 across `bl gsetband`, and s16-s31 are
       callee-saved, so this hook owes its own caller the same.

       THREE PAIRS, NOT TWO, from Multi Notch on. That mode designs four bands
       from shared controls, so a base index, a spread, a depth and a Q all have
       to survive three further `bl gsetband` calls on top of the frequency the
       loop recomputes each turn -- six floats live, and four saved registers do
       not hold six. Widening the save set is always safe: s16-s31 are
       callee-saved by AAPCS, so saving more of them can only ever be more
       correct, and three register pairs is 24 bytes, which keeps SP 8-byte
       aligned exactly as two pairs did.

       THE `vpush` AND THE `vpop` MUST CHANGE TOGETHER. A mismatched pair does
       not fail here; it corrupts the caller's FP register file on the way out,
       which is the same class of fault as the `s2` clobber b10 shipped and is
       far harder to see. */
    push.w  {{r4, r5, r6, r7, r8, lr}}
    vpush   {{d8, d9, d10}}
    mov     r4, r0                     /* the EQ object, handed to us */
    mov     r5, r1                     /* the block message */
    movw    r6, #{EQ_STORE & 0xffff}
    movt    r6, #{EQ_STORE >> 16}
    ldr     r3, [r6]
    movw    r0, #{EQ_MAGIC & 0xffff}
    movt    r0, #{EQ_MAGIC >> 16}
    cmp     r3, r0
    beq     gvalid
    mov     r2, r6
{_eq_seed("r0", "r1")}
gvalid:
{_eq_checksum("r0", "r6", "r3")}
    ldr     r3, [r6, #{EQ_S_SUM}]
    cmp     r0, r3
    bne.w   gout                       /* damaged store -> stock behaviour.
                                          THIS EXIT STAYS HERE, and it is not
                                          the bypass exit that moved into
                                          `gapply`. A failed checksum means we
                                          do not know what the store says, so
                                          writing the four bands from it is the
                                          wrong response -- leaving the object
                                          alone is. Bypass is the opposite
                                          case: the store is intact and it is
                                          telling us exactly what to build. */
    /* MASTER BYPASS IS NOT TESTED HERE. It used to be, and that is why it never
       bypassed anything: `gout` contains no store, so the four bands kept
       whatever the last `gapply` left them and the stock handler carried on
       filtering. The 50 ms smoother could not help, because a smoother ramps
       toward a TARGET and the target never changed. Bypass FROZE the EQ. The
       test now lives inside `gapply`, after `gclr` and before the mode
       dispatch, and takes the identity fill. See the note there.

       APPLY EVERY BLOCK, UNCONDITIONALLY -- the same shape as hook E, which
       stamps the compressor's store onto its live object every block and is
       the most robust thing in this patch.

       What used to be here was `cmp gen, agen / beq gout`: rebuild only when
       the generation moved. It looked like a free economy and it was not. The
       counter tracks OUR INTENT, not the OBJECT'S STATE. A preset load
       reconstructs the EQ object to constructor defaults and does not touch
       our patch RAM, so the store still said `agen == gen` and hook G took the
       early exit forever. The object stayed configured by nobody: audio
       passing, nothing happening, and a null test cannot see it, because every
       failure exits to pass-through and pass-through is what the test expects.

       `agen` is still written, so the store's shape and its checksum
       arithmetic do not change and nothing downstream has to learn a new
       layout. It simply stops being a gate.

       The cost was BOUNDED from the disassembly before this was changed, not
       asserted afterwards: 2,512 instructions / 5,444 cycles against a
       1,280,000-cycle block, 0.43%. D-25 is what makes that a real bound --
       the identity fill means `gapply` designs exactly four bands on every
       call in every mode, so the worst case is the typical case. Counts and
       method are in DEV-README. */
    ldr     r0, [r6, #{EQ_S_GEN}]
    str     r0, [r6, #{EQ_S_AGEN}]
    bl      gapply

gout:
    mov     r0, r4
    mov     r1, r5
    vpop    {{d8, d9, d10}}           /* the matching half of the prologue's
                                          save -- changed with it, never alone */
    pop.w   {{r4, r5, r6, r7, r8, lr}}
    .global gnext
gnext:
    .short 0,0                         /* b -> the stock handler */

/* ---- helpers ----------------------------------------------------------- */
gsetband:                              /* r0=band r1=type s0=freq s1=Q s2=gain dB */
    push    {{lr}}
    add.w   r2, r0, r0, lsl #2
    add.w   r2, r4, r2, lsl #2         /* obj + band*0x14 */
    strb.w  r1, [r2, #{EQ_TYPE_OFF:#x}]
    vstr    s2, [r2, #{EQ_GAIN_OFF:#x}]
    vstr    s0, [r2, #{EQ_FREQ_OFF:#x}]
    vstr    s1, [r2, #{EQ_Q_OFF:#x}]
    movs    r3, #1
    str.w   r3, [r2, #{EQ_EN_OFF:#x}]
    adr.w   r3, gdtab
    ldr.w   r3, [r3, r1, lsl #2]
    mov     r1, r0
    mov     r0, r4
    blx     r3                         /* 1010music's own filter designer */
    pop     {{pc}}

gftab:                                 /* s0 = index 0..16 -> s0 = Hz */
    movs    r0, #0
    vmov    s1, r0
    vcvt.f32.u32 s1, s1
    vcmpe.f32 s0, s1
    vmrs    APSR_nzcv, fpscr
    bpl     gft1
    vmov.f32 s0, s1
gft1:
    vmov.f32 s1, #16.0
    vcmpe.f32 s0, s1
    vmrs    APSR_nzcv, fpscr
    ble     gft2
    vmov.f32 s0, s1
gft2:
    vcvt.u32.f32 s1, s0
    vmov    r0, s1
    cmp     r0, #{EQ_TBL_N - 1}
    it      hi
    movhi   r0, #{EQ_TBL_N - 1}
    vmov    s1, r0
    vcvt.f32.u32 s1, s1
    vsub.f32 s1, s0, s1                /* frac */
    adr.w   r1, gtable
    add.w   r1, r1, r0, lsl #2
    vldr    s2, [r1]
    vldr    s3, [r1, #4]
    vsub.f32 s3, s3, s2
    vfma.f32 s2, s3, s1
    vmov.f32 s0, s2
    bx      lr

ghalf:                                 /* r0 = raw CC -> s0 = 0.0 .. 1.0, and
                                          0.0 for everything at or below the
                                          centre. This is Rule 1's mechanism
                                          for the unipolar controls: centre is
                                          neutral, the anticlockwise half is a
                                          plateau there, and the clockwise half
                                          carries the whole sweep. Monotonic,
                                          so Rule 2 still holds, and `Reset Mid`
                                          lands exactly on neutral. */
    movw    r1, #{EQ_CENTRE}
    subs    r0, r0, r1
    it      mi
    movmi   r0, #0
    vmov    s0, r0
    vcvt.f32.u32 s0, s0
    adr.w   r1, ghspan
    vldr    s1, [r1]
    vmul.f32 s0, s0, s1
    vmov.f32 s1, #1.0
    vcmpe.f32 s0, s1
    vmrs    APSR_nzcv, fpscr
    ble     ghdone
    vmov.f32 s0, s1
ghdone:
    bx      lr

gu16:                                  /* r0 = raw CC -> s0 = index 0..16 */
    vmov    s0, r0
    vcvt.f32.u32 s0, s0
{_f("s1", "gidxs", "r1")}
    vmul.f32 s0, s0, s1
    bx      lr

gqmap:                                 /* r0 = raw CC, r1 = &{{lo, span}} -> s0 */
    vldr    s0, [r1]
    vldr    s1, [r1, #4]
    vmov    s2, r0
    vcvt.f32.u32 s2, s2
{_f("s3", "gccinv", "r2")}
    vmul.f32 s2, s2, s3
    vfma.f32 s0, s2, s1
    bx      lr

ggain:                                 /* r0 = raw CC -> s0 = dB, flat at centre */
    movw    r1, #{EQ_CENTRE}
    subs    r2, r0, r1
    mov     r3, r2
    cmp     r3, #0
    it      lt
    rsblt   r3, r3, #0
    cmp     r3, #{EQ_DETENT}
    bls     ggflat
    subs    r3, r3, #{EQ_DETENT}
    vmov    s0, r3
    vcvt.f32.u32 s0, s0
{_f("s1", "ghsinv", "r1")}
    vmul.f32 s0, s0, s1
    cmp     r2, #0
    bgt     ggboost
{_f("s1", "ggcut", "r1")}
    b       ggend
ggboost:
{_f("s1", "ggboo", "r1")}
ggend:
    vmul.f32 s0, s0, s1
    bx      lr
ggflat:
    movs    r3, #0
    vmov    s0, r3
    vcvt.f32.u32 s0, s0
    bx      lr

gtiltd:                                /* r0 = raw CC -> s0 = dB, SYMMETRIC,
                                          flat at centre. ggain's shape with
                                          one difference: one constant instead
                                          of two, and a negate instead of a
                                          second table entry.

                                          Inside EQ_DETENT this returns exactly
                                          0.0, which is Rule 1's mechanism and
                                          the ONLY reason both of Tilt's shelves
                                          are flat with the knob centred.

                                          NO CLAMP, for ggain's exact reason:
                                          EQ_HALF_SPAN is precisely the travel
                                          available either side of the detent,
                                          so the widest input lands on exactly
                                          1.0 and the law reaches its stops
                                          without ever passing them. */
    movw    r1, #{EQ_CENTRE}
    subs    r2, r0, r1                 /* the SIGNED offset -- kept in r2,
                                          untouched by the _f loads below,
                                          which scratch r1 */
    mov     r3, r2
    cmp     r3, #0
    it      lt
    rsblt   r3, r3, #0                 /* and its magnitude, in r3 */
    cmp     r3, #{EQ_DETENT}
    bls     gtdflat
    subs    r3, r3, #{EQ_DETENT}
    vmov    s0, r3
    vcvt.f32.u32 s0, s0
{_f("s1", "ghsinv", "r1")}
    vmul.f32 s0, s0, s1                /* 0 .. 1 across the travel */
{_f("s1", "gtiltg", "r1")}
    vmul.f32 s0, s0, s1                /* 0 .. EQ_TILT_G decibels */
    cmp     r2, #0
    bgt     gtdend
    vneg.f32 s0, s0                    /* anticlockwise: equal and opposite */
gtdend:
    bx      lr
gtdflat:
    movs    r3, #0
    vmov    s0, r3
    vcvt.f32.u32 s0, s0
    bx      lr

gfshift:                               /* r0 = raw CC -> s0 = a FREQUENCY
                                          MULTIPLIER. EQ_FMT_DN at the
                                          anticlockwise stop, EQ_FMT_UP at the
                                          clockwise one, and EXACTLY 1.0 across
                                          the centre detent.

                                          `ggain`'s two-segment shape, for
                                          `ggain`'s reasons, with one difference
                                          that matters: the detent branch
                                          returns a LITERAL 1.0 rather than
                                          something that computes to nearly one.
                                          A shift of 0.999 would move every
                                          formant off the table by a hair with
                                          the knob centred, which is small
                                          enough to be inaudible and exactly the
                                          kind of thing that is later blamed on
                                          the table.

                                          TWO CONSTANTS, ONE `vfma`. The
                                          multiplier is 1.0 + span * u on both
                                          sides, with span -0.5 below centre and
                                          +1.0 above, so the stops land on 0.5
                                          and 2.0 -- one octave each way. That
                                          is `ggcut` / `ggboo`'s structure, so
                                          the two laws read against each other.

                                          NO CLAMP, for `ggain`'s exact reason:
                                          EQ_HALF_SPAN is precisely the travel
                                          available either side of the detent,
                                          so the widest input lands on exactly
                                          1.0 and the law reaches its stops
                                          without ever passing them.

                                          RULE 2: clockwise raises the formants,
                                          which is the direction a listener
                                          expects a voice to move. */
    movw    r1, #{EQ_CENTRE}
    subs    r2, r0, r1                 /* the SIGNED offset -- kept in r2,
                                          untouched by the _f loads below,
                                          which scratch r1 */
    mov     r3, r2
    cmp     r3, #0
    it      lt
    rsblt   r3, r3, #0                 /* and its magnitude, in r3 */
    cmp     r3, #{EQ_DETENT}
    bls     gfsflat
    subs    r3, r3, #{EQ_DETENT}
    vmov    s0, r3
    vcvt.f32.u32 s0, s0
{_f("s1", "ghsinv", "r1")}
    vmul.f32 s0, s0, s1                /* 0 .. 1 across the travel */
    cmp     r2, #0
    bgt     gfsup
{_f("s1", "gfmtdn", "r1")}
    b       gfsend
gfsup:
{_f("s1", "gfmtup", "r1")}
gfsend:
    vmov.f32 s2, #1.0
    vfma.f32 s2, s0, s1
    vmov.f32 s0, s2
    bx      lr
gfsflat:
    vmov.f32 s0, #1.0
    bx      lr

/* ---- apply: rebuild the whole mode ------------------------------------- */
gapply:
    push    {{lr}}
    movs    r0, #0
gclr:
    add.w   r1, r0, r0, lsl #2
    add.w   r1, r4, r1, lsl #2
    movs    r2, #0
    strb.w  r2, [r1, #{EQ_TYPE_OFF:#x}]
    str.w   r2, [r1, #{EQ_EN_OFF:#x}]
    adds    r0, #1
    cmp     r0, #4
    blo     gclr
    movs    r2, #0
    str     r2, [r6, #{EQ_S_MASK}]
    /* MASTER BYPASS, CC 57 -- tested HERE and nowhere else.
       `gclr` has just run, so all four bands are type 0 with enable 0 and the
       mask is 0. `gmout` therefore designs all four as an explicit identity
       Param at 0 dB, which 1010music's designer resolves to H(z) = 1 exactly:
       a bit-exact wire, max |b - a| = 0.000e+00 against the dry signal. It is
       the same path every unbuilt slot took on b10, which swept all ten
       detents with no pops and no clicks, so it is already hardware-proven
       silent.

       And because it is a TARGET change rather than a hard cut, the stock
       smoother at obj+0x168 ramps it in over 50 ms and back out again. No
       click going in, no ring coming out.

       THE TRAP, NAMED SO NOBODY TAKES IT LATER: do not "simplify" this to
       writing enable = 0 to all four bands. The stock handler then skips them
       and audio genuinely passes through, so it looks correct -- and that is
       exactly what makes it dangerous. A band that stops being enabled stops
       contributing INSTANTLY, which is b8's click, and resumes with stale
       coefficients and stale filter history, which is b7's ring. Both were
       rejected on hardware. For a KILL that shape was wrong because it passed
       audio; for a BYPASS passing audio is the whole point, which is precisely
       why the fault would ship unnoticed. The identity fill has neither. */
    ldr     r3, [r6, #{EQ_S_BYP}]
    cmp     r3, #0
    bne.w   gmout
    ldr     r3, [r6, #{EQ_S_MODE}]
    /* Ten slots, DJ Filter at 0 -- which is dial position 1, because the enum
       is zero-based and the dial is one-based. ALL TEN ARE NOW BUILT and get a
       compare each, so for the first time in this feature there is no unbuilt
       slot behind the chain. The trailing `b.w gmout` is what a value past the
       end would take, and it is the identity fill rather than a bare
       `pop {{pc}}` -- see the note on it below. There is no `Off` slot any more
       -- CC 57 is the master bypass and it does the job properly. Going from
       twelve slots to ten moved none of these values, so these compares are
       byte-identical across that change.

       The compares are in ENUM ORDER and new ones go in their numbered place
       rather than on the end: the chain is read against EQ-CONTROL-LAYOUT's
       dial far more often than it is executed, and a chain that runs in the
       dial's own order is one a reader can check by eye. */
    cmp     r3, #{EQ_M_DJ}
    beq.w   gmdj
    cmp     r3, #{EQ_M_DUALCUT}
    beq.w   gmtwin
    cmp     r3, #{EQ_M_BANDPASS}
    beq.w   gmphone
    cmp     r3, #{EQ_M_MIXER}
    beq.w   gmmix
    cmp     r3, #{EQ_M_TONE}
    beq.w   gmtone
    cmp     r3, #{EQ_M_TILT}
    beq.w   gmtilt
    cmp     r3, #{EQ_M_NOTCH}
    beq.w   gmnotch
    cmp     r3, #{EQ_M_PEAK}
    beq.w   gmpeak
    cmp     r3, #{EQ_M_MULTINOTCH}
    beq.w   gmmnot
    cmp     r3, #{EQ_M_FORMANT}
    beq.w   gmfmt
    b.w     gmout                      /* no slot left unbuilt: this is now
                                          unreachable for any value hook F can
                                          store, because the mode word is
                                          (raw * 10) >> 14 and cannot exceed 9.
                                          It stays because a store corrupted
                                          past its own checksum should land on
                                          the identity fill rather than on
                                          whatever follows. */

gmdj:                                  /* DJ Filter (slot 1). A sweeps the corner
                                          either side of a centre detent, B is
                                          resonance.
                                          D-18, RESOLVED and not to be
                                          re-opened: control C AND button C are
                                          unassigned in this mode. Drive was the
                                          only candidate for C and D-05 rules it
                                          out, and a knob that plainly does
                                          nothing is more honest than one
                                          duplicating its neighbour. So this
                                          routine reads neither EQ_S_GC nor
                                          EQ_S_BTN_C, CC 53 and CC 56 are inert
                                          here, and that absence is a decision.
                                          Button A forces control A to the
                                          centre value. The very next thing this
                                          routine does is subtract EQ_CENTRE and
                                          take `bls.w gmout` inside EQ_DETENT --
                                          so centre lands in the detent, and the
                                          detent is a REAL bypass: no band
                                          designed, mask left at 0 by gclr.
                                          That chain is deliberate. If the
                                          detent ever changes width, come back
                                          and check button A still bypasses. */
{ctl(EQ_S_GA, dest="r2")}\
    movw    r3, #{EQ_CENTRE}
    subs    r2, r2, r3
    mov     r0, r2
    cmp     r0, #0
    it      lt
    rsblt   r0, r0, #0
    cmp     r0, #{EQ_DETENT}
    bls.w   gmout                      /* in the detent: a real bypass */
    subs    r0, r0, #{EQ_DETENT}
    vmov    s0, r0
    vcvt.f32.u32 s0, s0
{_f("s1", "ghsinv", "r1")}
    vmul.f32 s0, s0, s1
    vmov.f32 s1, #1.0
    vcmpe.f32 s0, s1
    vmrs    APSR_nzcv, fpscr
    ble     gmdjc
    vmov.f32 s0, s1
gmdjc:
    cmp     r2, #0
    bgt     gmdjhp
    movs    r7, #{EQ_T_HCUT}           /* left: a low-pass, table backwards */
    vmov.f32 s1, #1.0
    vsub.f32 s0, s1, s0
    b       gmdjf
gmdjhp:
    movs    r7, #{EQ_T_LCUT}           /* right: a high-pass */
gmdjf:
    vmov.f32 s1, #16.0
    vmul.f32 s0, s0, s1
    vmov.f32 s18, s0                   /* keep the corner IN TABLE STEPS -- the
                                          second section is placed relative to
                                          it, and doing that in Hz would need a
                                          divide the table already avoids. */
    bl      gftab
    vmov.f32 s16, s0
    ldr     r0, [r6, #{EQ_S_GB}]
    adr.w   r1, gqres
    bl      gqmap
    vmov.f32 s17, s0
    movs    r0, #0
    mov     r1, r7
    vmov.f32 s0, s16
    vmov.f32 s1, s17
{zero}
    bl      gsetband
    movs    r2, #1
    str     r2, [r6, #{EQ_S_MASK}]
    /* CONTROL C IS THE SLOPE, and this supersedes D-18. D-18 locked C
       unassigned because drive was the only candidate and D-05 banned it;
       slope is pure biquad work, so the objection does not reach it.

       BE HONEST ABOUT WHAT THIS CONTROL DOES. Once two sections exist the
       ASYMPTOTIC slope is 24 dB/oct and stays there -- cascaded biquads
       quantise at 12 dB a stage and no amount of sweeping changes that. What
       varies continuously is the KNEE: how far up the band the second section
       sits, so how early the transition starts and how abruptly it bites.
       Converged on the first section is the hard 24 dB corner; spread away
       from it the response leans on one section near the corner and reads as
       roughly 12 dB there, tightening to 24 further out. That is a real and
       useful sweep and it is not a continuous slope, so do not label it one.

       Fully anticlockwise there is no second section at all, which IS a true
       12 dB/oct -- the b5 behaviour, still reachable, deliberately kept.

       CC 59 rides on top through the ordinary `ctl` forced-value path: held,
       control C reads as maximum and the knee snaps hard 24. Released, the
       encoder's own position returns untouched with no jump. That needs no
       special case in hook F -- see the note in the return for why routing
       CC 53 into EQ_S_SLOPE was considered and rejected. */
{ctl(EQ_S_GC, EQ_S_SLOPE, EQ_FORCE_MAX, dest="r0")}
    bl      gu16
    vmov.f32 s1, #1.0
    vcmpe.f32 s0, s1
    vmrs    APSR_nzcv, fpscr
    blt.w   gmout                      /* bottom of C: one section, a true 12 */
    vmov.f32 s1, #16.0
    vsub.f32 s0, s1, s0                /* clockwise converges, so 16 - index */
{_f("s1", "gdjspr", "r2")}
    vmul.f32 s0, s0, s1                /* the offset, in table steps */
    cmp     r7, #{EQ_T_HCUT}
    beq     gmdjs1
    vsub.f32 s0, s18, s0               /* high-pass: spread downward */
    b       gmdjs2
gmdjs1:
    vadd.f32 s0, s18, s0               /* low-pass: spread upward */
gmdjs2:
    bl      gftab
    vmov.f32 s16, s0
    movs    r0, #1                     /* second section, non-resonant */
    mov     r1, r7
    vmov.f32 s0, s16
{_f("s1", "gqfix", "r2")}
{zero}
    bl      gsetband
    movs    r2, #3
    str     r2, [r6, #{EQ_S_MASK}]
    b.w     gmout

gmmix:
    bl      gmix3
    movs    r2, #7
    str     r2, [r6, #{EQ_S_MASK}]
    b.w     gmout

gmix3:                                 /* bands 0-2: low shelf, mid bell, high shelf.
                                          RULE 3: control A is HIGH, C is LOW.
                                          The band table stays low-to-high --
                                          only the control each band reads is
                                          reversed, so gmixf/gmixq/gmixt need no
                                          reordering and stay legible.

                                          THE PUSHES ARE DJ KILLS, and b6 had
                                          them backwards. b6 killed a band by
                                          leaving it DISABLED, reasoning that a
                                          disabled band passes nothing. That is
                                          true of the BAND and false of the
                                          AUDIO: a disabled band applies no
                                          filter, so the signal passes straight
                                          through. Worse, Rule 1 puts every band
                                          at 0 dB at centre, so at rest the kill
                                          was removing a filter that was already
                                          transparent and did nothing audible at
                                          all. The user found exactly that --
                                          the kills only worked once an encoder
                                          had been turned away from centre.

                                          A kill must REMOVE that part of the
                                          spectrum, dramatically, FROM CENTRE,
                                          which is where the knobs rest. So a
                                          held kill swaps the band's TYPE rather
                                          than nudging its gain: low becomes a
                                          high-pass at 150 Hz, high becomes a
                                          low-pass at 4 kHz, mid becomes a wide
                                          -24 dB scoop at 700 Hz. Attenuating
                                          instead would not do it -- ggain's
                                          floor is -24 dB and a shelf at -24 dB
                                          still passes everything past its
                                          corner.

                                          The band is always designed either
                                          way, so the mask is always 7 and the
                                          stored encoder value is never written.
                                          Release returns the sound to exactly
                                          where the knob is: D-03, unchanged. */
    push    {{lr}}
    movs    r7, #0
gmix3l:
    adr.w   r0, gmixf
    add.w   r0, r0, r7, lsl #2
    vldr    s16, [r0]                  /* the corner, the same either way */
    movs    r0, #{EQ_S_BTN_C}
    sub.w   r0, r0, r7, lsl #2         /* band 0 -> push C, 1 -> B, 2 -> A */
    ldr     r0, [r6, r0]
    cmp     r0, #0
    bne     gmix3k
    movs    r0, #{EQ_S_GC}
    sub.w   r0, r0, r7, lsl #2         /* band 0 -> control C, 2 -> control A */
    ldr     r0, [r6, r0]
    bl      ggain
    vmov.f32 s18, s0
    adr.w   r0, gmixq
    add.w   r0, r0, r7, lsl #2
    vldr    s17, [r0]
    adr.w   r0, gmixt
    ldrb    r1, [r0, r7]
    b       gmix3s
gmix3k:                                /* held: the kill shape for this band */
    adr.w   r0, gmixkq
    add.w   r0, r0, r7, lsl #2
    vldr    s17, [r0]
    adr.w   r0, gmixkg
    add.w   r0, r0, r7, lsl #2
    vldr    s18, [r0]
    adr.w   r0, gmixk
    ldrb    r1, [r0, r7]
gmix3s:
    mov     r0, r7
    vmov.f32 s0, s16
    vmov.f32 s1, s17
    vmov.f32 s2, s18
    bl      gsetband
    adds    r7, #1
    cmp     r7, #3
    blo     gmix3l
    pop     {{pc}}

gmtone:                                /* Tone + Filter (slot 4, dial position
                                          5). A is the HIGH shelf, B is the LOW
                                          shelf, C is a bipolar filter with a
                                          centre detent.

                                          D-14 reshaped `Full desk` into this,
                                          and it is the only built mode the
                                          three-encoder budget genuinely broke.
                                          Losing the mid band buys tone AND a
                                          filter on one panel with no mode
                                          change, which is worth more live than
                                          three gains and an unreachable filter.

                                          RULE 3: control A is HIGH. That is the
                                          same correction b6 made to Mixer EQ
                                          after the user found it reading upside
                                          down inside the first minute of play,
                                          and it is not negotiable by preference.

                                          THE TWO PUSHES ARE KILLS, and they are
                                          literally two of Mixer EQ's three --
                                          the same gmixk / gmixkq / gmixkg
                                          shapes at the same corners, which is
                                          why this routine sits directly under
                                          `gmix3` rather than in dial order.

                                          A KILL SWAPS THE BAND'S TYPE and still
                                          calls `gsetband`; it never disables a
                                          band and never omits a mask bit. That
                                          is D-25, and it is what makes the
                                          gesture dramatic FROM CENTRE, which is
                                          where the knobs rest under Rule 1. b6
                                          killed by leaving the band DISABLED:
                                          a disabled band applies no filter, so
                                          the audio passed straight through and
                                          the kill removed something that was
                                          already transparent. Attenuating is no
                                          better -- `ggain`'s floor is -24 dB
                                          and a shelf at -24 dB still passes
                                          everything past its corner.

                                          RULE 1: both shelves take `ggain`,
                                          exactly 0.0 dB in the detent, and the
                                          filter is not engaged at all inside
                                          its own. Centred knobs give two flat
                                          shelves and two undesigned bands --
                                          the EQ doing nothing. */
    /* BAND 0 -- the LOW shelf, on control B, killed by push B. The corner is
       gmixf index 0, 150 Hz, and it is the same corner either way. */
    adr.w   r0, gmixf
    vldr    s16, [r0]
    ldr     r3, [r6, #{EQ_S_BTN_B}]    /* D-03: read the button, write nothing */
    cmp     r3, #0
    bne     gmtnk0
    ldr     r0, [r6, #{EQ_S_GB}]
    bl      ggain
    vmov.f32 s18, s0                   /* the gain goes to the CALLEE-saved s18,
                                          which is `gmix3`'s own shape and what
                                          hook G's `vpush {{d8, d9, d10}}`
                                          exists for.
                                          Nothing between here and `bl gsetband`
                                          calls anything either way. */
    adr.w   r0, gmixq
    vldr    s17, [r0]
    adr.w   r0, gmixt
    ldrb    r1, [r0]                   /* EQ_T_LSHELF */
    b       gmtns0
gmtnk0:                                /* held: kill low -> a high-pass at 150 Hz */
    adr.w   r0, gmixkq
    vldr    s17, [r0]
    adr.w   r0, gmixkg
    vldr    s18, [r0]
    adr.w   r0, gmixk
    ldrb    r1, [r0]                   /* EQ_T_LCUT */
gmtns0:
    movs    r0, #0
    vmov.f32 s0, s16
    vmov.f32 s1, s17
    vmov.f32 s2, s18
    bl      gsetband
    /* BAND 1 -- the HIGH shelf, on control A, killed by push A. Index 2 of the
       same five tables: 4 kHz, and the kill there is a low-pass. */
    adr.w   r0, gmixf
    vldr    s16, [r0, #8]
    ldr     r3, [r6, #{EQ_S_BTN_A}]
    cmp     r3, #0
    bne     gmtnk1
    ldr     r0, [r6, #{EQ_S_GA}]
    bl      ggain
    vmov.f32 s18, s0
    adr.w   r0, gmixq
    vldr    s17, [r0, #8]
    adr.w   r0, gmixt
    ldrb    r1, [r0, #2]               /* EQ_T_HSHELF */
    b       gmtns1
gmtnk1:                                /* held: kill high -> a low-pass at 4 kHz */
    adr.w   r0, gmixkq
    vldr    s17, [r0, #8]
    adr.w   r0, gmixkg
    vldr    s18, [r0, #8]
    adr.w   r0, gmixk
    ldrb    r1, [r0, #2]               /* EQ_T_HCUT */
gmtns1:
    movs    r0, #1
    vmov.f32 s0, s16
    vmov.f32 s1, s17
    vmov.f32 s2, s18
    bl      gsetband
    movs    r2, #3
    str     r2, [r6, #{EQ_S_MASK}]     /* WRITTEN HERE, so the mode is complete
                                          and correct even if the filter turns
                                          out to be sitting in its detent */
    /* BAND 2 -- the filter. THIS BIPOLAR LAW IS A SECOND COPY OF `gmdj`'s
       control A, written out line for line, and the two have to be changed
       together. It is deliberately not shared. The two detent branches do
       different things: `gmdj`'s dead band is a whole-mode bypass that leaves
       the mask at 0, while this one still has two shelves running and a mask of
       3. D-30 also puts `gmdj` out of bounds for reworking here -- the five
       modes that are already playing are not touched by this plan.

       C's push forces the RAW CENTRE, which is Rule 4's OPEN gesture: held, the
       filter stops acting and lets everything through; released, the encoder's
       stored position comes back untouched, so nothing jumps.

       THE FILTER IS DELIBERATELY NON-RESONANT. There is no fourth encoder for
       resonance in this mode and tone wins the two it has. DJ Filter is where
       resonance lives, and putting it here would cost a shelf. Not forgotten --
       decided. */
{ctl(EQ_S_GC, EQ_S_BTN_C, EQ_FORCE_CENTRE, dest="r2")}
    movw    r3, #{EQ_CENTRE}
    subs    r2, r2, r3
    mov     r0, r2
    cmp     r0, #0
    it      lt
    rsblt   r0, r0, #0
    cmp     r0, #{EQ_DETENT}
    bls.w   gmout                      /* inside the detent the filter is not
                                          engaged at all, and bands 2 and 3 take
                                          `gmout`'s identity fill (D-25) rather
                                          than being switched off. That is what
                                          makes crossing the detent a 50 ms ramp
                                          instead of a click. The mask stays 3. */
    subs    r0, r0, #{EQ_DETENT}
    vmov    s0, r0
    vcvt.f32.u32 s0, s0
{_f("s1", "ghsinv", "r1")}
    vmul.f32 s0, s0, s1
    vmov.f32 s1, #1.0
    vcmpe.f32 s0, s1
    vmrs    APSR_nzcv, fpscr
    ble     gmtnc
    vmov.f32 s0, s1
gmtnc:
    cmp     r2, #0
    bgt     gmtnhp
    movs    r7, #{EQ_T_HCUT}           /* left: a low-pass, index taken backwards
                                          -- anticlockwise closes to a thump */
    vmov.f32 s1, #1.0
    vsub.f32 s0, s1, s0
    b       gmtnf
gmtnhp:
    movs    r7, #{EQ_T_LCUT}           /* right: a high-pass -- clockwise thins
                                          out to hats */
gmtnf:
    vmov.f32 s1, #16.0
    vmul.f32 s0, s0, s1
    bl      gftab
    vmov.f32 s16, s0                   /* the corner in Hz, CALLEE-saved, so the
                                          second section converges on it without
                                          designing it a second time */
{_f("s17", "gqfix", "r2")}
    movs    r0, #2
    mov     r1, r7
    vmov.f32 s0, s16
    vmov.f32 s1, s17
{zero}
    bl      gsetband
    movs    r2, #7
    str     r2, [r6, #{EQ_S_MASK}]
    /* BAND 3 -- CC 59's cascade: a CONVERGED second section at the same corner,
       exactly as `gmtwin` does it. BE HONEST ABOUT IT. Once two sections exist
       the asymptotic slope is 24 dB/oct and stays there, and converged sections
       are the hard corner. There is NO variable knee in this mode -- that is DJ
       Filter's encoder 4, it costs a whole encoder, and all three are spent. */
    ldr     r3, [r6, #{EQ_S_SLOPE}]
    cmp     r3, #0
    beq.w   gmout
    movs    r0, #3
    mov     r1, r7
    vmov.f32 s0, s16
    vmov.f32 s1, s17
{zero}
    bl      gsetband
    movs    r2, #15
    str     r2, [r6, #{EQ_S_MASK}]
    b.w     gmout

gmtwin:                                /* Dual Cut (slot 1).
                                          RULE 3: A is the HIGH cut, B is the
                                          LOW cut -- high sits at the top. C is
                                          the resonance both sections share.
                                          RULE 1: centre opens both cuts
                                          OUTWARD, high cut to 12 kHz and low
                                          cut to 40 Hz, so centred knobs pass
                                          everything. Through b5 both cuts
                                          landed on 692.8 Hz at centre and
                                          cancelled to silence.
                                          The law is `ghalf`: the anticlockwise
                                          half is a plateau at fully open, and
                                          the clockwise half closes the cut.
                                          Monotonic, and `Reset Mid` is a real
                                          bypass for that section. */
{ctl(EQ_S_GC, dest="r0")}
    adr.w   r1, gqres
    bl      gqmap
    vmov.f32 s17, s0                   /* shared resonance */
{ctl(EQ_S_GA, EQ_S_BTN_A, EQ_FORCE_CENTRE, dest="r0")}
    bl      ghalf
    vmov.f32 s1, #16.0
    vmul.f32 s0, s0, s1
    vmov.f32 s1, #16.0
    vsub.f32 s0, s1, s0                /* open = 16 (12 kHz), closed = 0 */
    bl      gftab
    vmov.f32 s16, s0
    vmov.f32 s19, s0                   /* keep it for the 24 dB/oct cascade */
{setband(0, EQ_T_HCUT)}
{ctl(EQ_S_GB, EQ_S_BTN_B, EQ_FORCE_CENTRE, dest="r0")}
    bl      ghalf
    vmov.f32 s1, #16.0
    vmul.f32 s0, s0, s1                /* open = 0 (40 Hz), closed = 16 */
    bl      gftab
    vmov.f32 s18, s0                   /* keep it for the cascade too */
    vmov.f32 s16, s0
{setband(1, EQ_T_LCUT)}
    movs    r2, #3
    str     r2, [r6, #{EQ_S_MASK}]
    ldr     r3, [r6, #{EQ_S_SLOPE}]
    cmp     r3, #0
    beq.w   gmout
    vmov.f32 s16, s19                  /* 24 dB/oct: a second, non-resonant */
{_f("s17", "gqfix", "r2")}
{setband(2, EQ_T_HCUT)}
    vmov.f32 s16, s18
{_f("s17", "gqfix", "r2")}
{setband(3, EQ_T_LCUT)}
    movs    r2, #15
    str     r2, [r6, #{EQ_S_MASK}]
    b.w     gmout

gmphone:                               /* Band Pass (slot 2). A drags the centre,
                                          B sets the width.
                                          RULE 1: centre is MAXIMUM width -- a
                                          half-width of 8 table steps either
                                          side, which gftab clamps to the ends
                                          of the range, so centred knobs pass
                                          everything. Through b5 centre gave a
                                          202-2451 Hz band. Clockwise narrows.
                                          C and its push are unassigned. */
{ctl(EQ_S_GA, dest="r0")}
    bl      gu16
    vmov.f32 s18, s0                   /* centre, in table steps */
{ctl(EQ_S_GB, EQ_S_BTN_B, EQ_FORCE_CENTRE, dest="r0")}
    bl      ghalf
{_f("s1", "gbpnar", "r1")}
    vmul.f32 s0, s0, s1                /* narrowing, 0 .. (8 - 0.4) */
{_f("s1", "gbpwid", "r1")}
    vsub.f32 s19, s1, s0               /* half-width, 8.0 down to 0.4 */
{_f("s17", "gqbell", "r2")}
    vsub.f32 s0, s18, s19
    bl      gftab
    vmov.f32 s16, s0
{setband(0, EQ_T_LCUT)}
{_f("s17", "gqbell", "r2")}
    vadd.f32 s0, s18, s19
    bl      gftab
    vmov.f32 s16, s0
{setband(1, EQ_T_HCUT)}
    movs    r2, #3
    str     r2, [r6, #{EQ_S_MASK}]
    /* CONTROL C IS GAIN, which turns Band Pass into isolate-and-boost: cut
       everything outside the band, then lift what is left. Distinct from the
       unbuilt Peak mode, which boosts WITHOUT cutting its surroundings.

       THE BAND BUDGET IS FOUR AND THIS MODE NOW WANTS FIVE. Two cuts, one
       gain bell, and a symmetric 24 dB/oct cascade needs two more. So the two
       compete, and gain wins when it is asked for:

         gain flat (C in the detent)  -> bands 2 and 3 are free, and CC 59
                                         cascades both edges exactly as in b6
         gain dialled in              -> band 2 is the bell and CC 59 is
                                         ignored in this mode

       Gain flat is the resting state, so CC 59 keeps working everywhere the
       performer has not deliberately asked for a boost. The alternative --
       dropping the cascade outright -- would have cost a capability that
       already works on hardware, for a case that only arises once you turn
       control C. Rule 1 is safe either way: the detent is flat, so at centre
       no bell is designed at all. */
    ldr     r0, [r6, #{EQ_S_GC}]
    movw    r3, #{EQ_CENTRE}
    subs    r0, r0, r3
    cmp     r0, #0
    it      lt
    rsblt   r0, r0, #0
    cmp     r0, #{EQ_DETENT}
    bls     gmbpsl                     /* flat: the cascade may have the bands */
    /* ORDER MATTERS HERE, AND IT IS THE WHOLE OF THE b10 FAULT.

       s0-s15 are caller-saved: a callee may destroy any of them and owes the
       caller nothing. s16-s31 are callee-saved, which is why hook G's prologue
       `vpush {{d8, d9, d10}}` and why every other mode routine parks its
       frequency, its Q and its gain in s16..s21 across a call. This routine improvised,
       and that is the defect -- not the maths, not the mode, not the remap.

       What b7 (`61efbede`) shipped, and b10 was simply the first image to put
       on hardware: the gain was computed FIRST and parked in s2, then `gftab`
       was called. `gftab` ends `vldr s2, [r1]`, so s2 came back holding the
       CENTRE FREQUENCY IN HERTZ. `gsetband`'s ABI is s2 = gain in dB, so the
       bell was designed at 40 to 12000 dB where at most +9 was asked for. The
       `vmov.f32 s2, s2` that used to sit two instructions above `bl gsetband`
       was a NO-OP that read as intent, which is how it survived review.

       The fix is a reorder, not a relocation: compute the frequency first and
       keep it in s16, then compute the gain LAST, so nothing whatsoever runs
       between `bl ggain` and `bl gsetband` and there is no lifetime to get
       wrong. This is now the same shape as every other mode routine. The five
       modes still to be built use this same compute-a-gain-then-call-gsetband
       pattern; copy THIS one. */
    vmov.f32 s0, s18                   /* the bell sits on the centre frequency */
    bl      gftab
    vmov.f32 s16, s0                   /* the bell's centre -- CALLEE-saved */
{_f("s17", "gqbell", "r2")}
    ldr     r0, [r6, #{EQ_S_GC}]
    bl      ggain                      /* gain LAST: nothing between here and
                                          gsetband, so nothing can destroy it */
    vmov.f32 s2, s0
    movs    r0, #2
    movs    r1, #{EQ_T_PARAM}
    vmov.f32 s0, s16
    vmov.f32 s1, s17
    bl      gsetband
    movs    r2, #7
    str     r2, [r6, #{EQ_S_MASK}]
    b.w     gmout
gmbpsl:
    ldr     r3, [r6, #{EQ_S_SLOPE}]
    cmp     r3, #0
    beq.w   gmout
{_f("s17", "gqfix", "r2")}
    vsub.f32 s0, s18, s19              /* 24 dB/oct: cascade both edges */
    bl      gftab
    vmov.f32 s16, s0
{setband(2, EQ_T_LCUT)}
{_f("s17", "gqfix", "r2")}
    vadd.f32 s0, s18, s19
    bl      gftab
    vmov.f32 s16, s0
{setband(3, EQ_T_HCUT)}
    movs    r2, #15
    str     r2, [r6, #{EQ_S_MASK}]
    b.w     gmout

gmnotch:                               /* Notch (slot 6). A is the frequency, B is
                                          Q, C is DEPTH.
                                          RULE 1: centre is ZERO depth, so the
                                          band is designed but transparent and
                                          centred knobs pass everything.
                                          Through b5 depth was a -24 dB
                                          constant and centre gave a deep notch
                                          at 692.8 Hz. Giving C the depth is
                                          also what finally gives that encoder
                                          real work in this mode.
                                          C's push forces maximum depth -- the
                                          slam-the-notch gesture, and the one
                                          button here that does something
                                          `Reset Mid` cannot. */
{ctl(EQ_S_GA, dest="r0")}
    bl      gu16
    bl      gftab
    vmov.f32 s16, s0
{ctl(EQ_S_GB, dest="r0")}
    adr.w   r1, gqnot
    bl      gqmap
    vmov.f32 s17, s0
{ctl(EQ_S_GC, EQ_S_BTN_C, EQ_FORCE_MAX, dest="r0")}
    bl      ghalf
{_f("s1", "gnotchg", "r1")}
    vmul.f32 s18, s0, s1               /* 0 dB at centre, -24 dB at full CW */
{setband(0, EQ_T_PARAM, gain="    vmov.f32 s2, s18")}
    movs    r2, #1
    str     r2, [r6, #{EQ_S_MASK}]
    b.w     gmout

gmtilt:                                /* Tilt (slot 5, dial position 6). A is
                                          the tilt, B is the hinge, C and its
                                          push are unassigned.

                                          TWO SHELVES ON ONE FREQUENCY, GEARED
                                          TOGETHER. There is no tilt designer
                                          type and there does not need to be:
                                          a low shelf and a high shelf sharing
                                          a corner, given equal and opposite
                                          gain, IS a tilt. Clockwise lifts the
                                          treble and drops the bass by the same
                                          number of decibels, so the see-saw
                                          pivots rather than the whole level
                                          moving -- which is exactly what a
                                          performer expects of a tone tilt and
                                          what an asymmetric law would break.
                                          That is why this calls `gtiltd` and
                                          not `ggain`.

                                          RULE 1: `gtiltd` returns exactly 0.0
                                          inside the detent, so both shelves are
                                          designed and both are flat. The hinge
                                          needs NO centre handling at all -- a
                                          shelf at 0 dB is transparent whatever
                                          its corner is, so only the control
                                          that maps to gain is Rule 1's problem.

                                          Bands 2 and 3 are not touched, and
                                          `gmout` gives them the identity fill
                                          (D-25). This routine reads neither
                                          EQ_S_GC, EQ_S_BTN_B nor EQ_S_BTN_C:
                                          CCs 53, 55 and 56 are inert here, and
                                          that absence is a decision the
                                          simulator's unread-control check turns
                                          into an executable claim. */
{ctl(EQ_S_GA, EQ_S_BTN_A, EQ_FORCE_MAX, dest="r0")}
    bl      gtiltd
    vmov.f32 s18, s0                   /* the tilt, in dB. s18 is CALLEE-saved,
                                          so it survives the two calls between
                                          here and the second `gsetband`. Both
                                          shelves need it, so it HAS to outlive
                                          a call -- parking it in s2 the way b10
                                          did would put the hinge frequency in
                                          the gain slot. Copy gmphone's order,
                                          never b10's. */
{ctl(EQ_S_GB, dest="r0")}
    bl      gu16
    bl      gftab
    vmov.f32 s16, s0                   /* the hinge -- BOTH shelves share it,
                                          which is what makes the pivot a pivot
                                          rather than two unrelated shelves */
{_f("s17", "gqfix", "r2")}
{setband(0, EQ_T_LSHELF, gain="    vneg.f32 s2, s18")}
{setband(1, EQ_T_HSHELF, gain="    vmov.f32 s2, s18")}
    movs    r2, #3
    str     r2, [r6, #{EQ_S_MASK}]
    b.w     gmout

gmpeak:                                /* Peak (slot 7, dial position 8). A is
                                          the frequency, B is the Q, C is the
                                          gain. One band; three live encoders.

                                          PEAK BOOSTS WITHOUT CUTTING ANYTHING
                                          AROUND IT, and that is the whole
                                          distinction from Band Pass. Band Pass
                                          cuts everything outside its window
                                          first and then lifts what is left;
                                          this touches nothing but the bell.
                                          They sound nothing alike and they are
                                          reached from different dial positions
                                          on purpose. Do not "unify" them.

                                          RULE 1: `ggain` is exactly 0.0 dB in
                                          the centre detent, so the band is
                                          designed and transparent. Frequency
                                          and Q are free at centre for the same
                                          reason Tilt's hinge is.

                                          Button A is unread -- CC 54 is inert
                                          here, deliberately. B forces maximum Q
                                          and C maximum boost: both are Rule 4
                                          hold-to-slam, both forced in the RAW
                                          CC domain so they take the same curve
                                          the encoder would, and neither writes
                                          the stored value, so release hands
                                          back with no jump. */
{ctl(EQ_S_GA, dest="r0")}
    bl      gu16
    bl      gftab
    vmov.f32 s16, s0
{ctl(EQ_S_GB, EQ_S_BTN_B, EQ_FORCE_MAX, dest="r0")}
    adr.w   r1, gqres
    bl      gqmap
    vmov.f32 s17, s0
{ctl(EQ_S_GC, EQ_S_BTN_C, EQ_FORCE_MAX, dest="r0")}
    bl      ggain                      /* the gain LAST, and no call whatsoever
                                          between here and `gsetband` -- this is
                                          gmphone's corrected order and it is
                                          the shape every remaining mode copies */
    vmov.f32 s18, s0
{setband(0, EQ_T_PARAM, gain="    vmov.f32 s2, s18")}
    movs    r2, #1
    str     r2, [r6, #{EQ_S_MASK}]
    b.w     gmout

gmmnot:                                /* PHASER on the panel (slot 8, dial
                                          position 9). A sweeps the whole stack,
                                          B is the depth, C is the spread.

                                          THE SHAPE IS A PHASER'S; THE
                                          MECHANISM IS FOUR `gsetband` CALLS.
                                          D-15 renamed the slot away from
                                          `Phaser` on a DSP argument that still
                                          holds: a real phaser is a cascade of
                                          all-pass sections with feedback, which
                                          is new per-sample DSP and D-19 bans it
                                          outright. NONE OF THAT IS REOPENED
                                          HERE. There is still no all-pass, no
                                          feedback and no per-sample code --
                                          four Param bands, four calls, no
                                          filter state of our own.

                                          What changed at b14 is the LABEL, and
                                          the performer's argument for it is
                                          about sound rather than topology: a
                                          small number of
                                          non-harmonically-related notches
                                          sweeping together as one body IS the
                                          audible signature of a phaser, and
                                          that is what came off the module. It
                                          is emphatically NOT a comb -- a comb's
                                          notches are harmonically locked and
                                          sound metallic and tuned, and these
                                          are neither. It also sounds nothing
                                          like the single Notch at dial 7, which
                                          is what earns it a slot.

                                          THE SPACING IS A RATIO, NOT AN
                                          INTERVAL COUNT. `gftab`'s table is
                                          logarithmic, so a fixed number of
                                          table steps is a fixed RATIO between
                                          neighbours, and geometric spacing can
                                          never be a comb's arithmetic harmonic
                                          series. What is NOT automatic is
                                          landing away from a musical ratio;
                                          EQ_MN_SP_LO / EQ_MN_SP_HI carry that
                                          arithmetic and the reason for it, and
                                          no offline gate has an opinion on it.

                                          RULE 1 RESTS ENTIRELY ON THE DEPTH.
                                          A Param band at 0 dB is H(z) = 1
                                          whatever its frequency and its Q are,
                                          so `ghalf` returning exactly 0.0 at
                                          and below centre makes all four bands
                                          transparent wherever sweep and spread
                                          happen to sit. Those two carry no
                                          Rule 1 obligation at all.

                                          ALL FOUR BANDS ARE DESIGNED ON EVERY
                                          REBUILD and the mask is always 15, so
                                          `gmout` finds nothing to fill. That is
                                          D-25 satisfied rather than dodged: no
                                          band is ever switched off, and a notch
                                          is made inaudible by its depth
                                          reaching 0 dB, never by being skipped.

                                          BUTTON A IS UNREAD -- CC 54 is inert
                                          here, deliberately, and that absence is
                                          a decision the simulator's
                                          unread-control check turns into an
                                          executable claim. */
{ctl(EQ_S_GA, dest="r0")}
    bl      gu16
    vmov.f32 s19, s0                   /* the base index, in table steps. s18,
                                          s19 and s20 are all CALLEE-saved,
                                          because every one of them has to
                                          survive FOUR `bl gsetband` calls and
                                          s0-s15 would not survive one. This is
                                          `gmtilt`'s case, not `gmphone`'s:
                                          there is no reordering that removes
                                          the hazard, so the values go where
                                          `vpush {{d8, d9, d10}}` protects them. */
{ctl(EQ_S_GC, EQ_S_BTN_C, EQ_FORCE_MAX, dest="r0")}
    adr.w   r1, gmnsp
    bl      gqmap                      /* the spread, in table steps: linear
                                          over the whole control range, because
                                          it has no Rule 1 obligation and so
                                          does not want `ghalf`'s plateau */
    vmov.f32 s20, s0
{ctl(EQ_S_GB, EQ_S_BTN_B, EQ_FORCE_MAX, dest="r0")}
    bl      ghalf
{_f("s1", "gnotchg", "r1")}
    vmul.f32 s18, s0, s1               /* 0 dB at centre, -24 dB at full CW --
                                          `gmnotch`'s depth law, unchanged */
{_f("s17", "gqmn", "r1")}              /* one fixed Q, shared by all four */
    movs    r2, #15
    str     r2, [r6, #{EQ_S_MASK}]     /* WRITTEN BEFORE THE LOOP: all four bands
                                          are designed unconditionally, so 15 is
                                          correct from the first instruction and
                                          no early exit added later can leave the
                                          mode half-described */
    movs    r7, #0
gmmnl:
    vmov    s0, r7
    vcvt.f32.u32 s0, s0
    vmov.f32 s1, #1.5
    vsub.f32 s0, s0, s1                /* -1.5, -0.5, +0.5, +1.5 ... */
    vmul.f32 s0, s0, s20               /* ... spreads from the centre, so the */
    vadd.f32 s0, s0, s19               /* stack stays symmetric about the sweep */
    bl      gftab                      /* -> Hz, and it CLAMPS the table's ends,
                                          so a stack driven off either end lands
                                          on 40 Hz or 12 kHz rather than wrapping */
    vmov.f32 s16, s0
    mov     r0, r7
    movs    r1, #{EQ_T_PARAM}
    vmov.f32 s0, s16
    vmov.f32 s1, s17
    vmov.f32 s2, s18
    bl      gsetband                   /* unconditional, all four times: there is
                                          no branch anywhere in this routine that
                                          reaches EQ_S_MASK while skipping a band */
    adds    r7, #1
    cmp     r7, #4
    blo     gmmnl
    b.w     gmout

gmfmt:                                 /* Formant (slot 9, dial position 10).
                                          A is the vowel, B the intensity, C an
                                          octave shift on all three formants at
                                          once.

                                          THREE BELLS PARKED ON THE FIRST THREE
                                          RESONANCES OF A VOCAL TRACT, morphing
                                          continuously I -> E -> A -> O -> U.
                                          The frequencies are Peterson and
                                          Barney's own measurements -- see
                                          EQ_FMT_TABLE for the citation, for how
                                          the rows were computed, and for the
                                          label discrepancy that had to be
                                          settled before a single instruction of
                                          this routine was written. They are the
                                          only constants in this patch that came
                                          from outside it.

                                          THE ORDER IS D-29 AND IT IS THE WHOLE
                                          DESIGN. Front to back, so every step
                                          is between ADJACENT vowels and every
                                          intermediate knob position interpolates
                                          into a real vowel rather than into a
                                          chord of two unrelated ones. A
                                          different ordering would still sweep;
                                          it would just stop sounding like
                                          speech in between.

                                          THE INTERPOLATION IS CONTROL RATE AND
                                          MUST STAY THAT WAY. It runs here,
                                          inside `gapply`, once per accepted
                                          change of the generation counter --
                                          which is where DJ Filter's knee
                                          spread, Band Pass's half-width and
                                          Dual Cut's bipolar corner already do
                                          their sums. D-19 forbids the arm
                                          computing anything per sample or
                                          reading a sample buffer, and nothing
                                          here does either: the result of every
                                          sum is handed to an ordinary
                                          `gsetband`, and 1010music's designer
                                          and its 50 ms smoother do the rest. If
                                          a future session wants the morph
                                          "smoother" and reaches for a frame
                                          loop, THE SMOOTHER IS ALREADY THERE,
                                          on the other side of `gsetband`, and
                                          a frame loop is what caused every
                                          failure of 2026-09-04.

                                          RULE 1 RESTS ENTIRELY ON THE
                                          INTENSITY, exactly as Phaser's rests
                                          on its depth. A Param at 0 dB is
                                          H(z) = 1 whatever its frequency and Q
                                          are, so `ghalf` returning exactly 0.0
                                          at and below centre makes all three
                                          bells transparent wherever the vowel
                                          and the shift happen to sit. Those two
                                          carry no Rule 1 obligation at all,
                                          which is what lets the vowel rest on a
                                          real vowel instead of on a boundary.

                                          BAND 3 IS NOT SKIPPED. The mask is 3
                                          bands' worth, 7, and band 3 falls
                                          through to `gmout`'s identity fill.
                                          That is D-25: no band is ever switched
                                          off, a band a mode does not want is a
                                          flat Param, and the stock smoother
                                          ramps every transition in and out.

                                          BUTTONS A AND C ARE UNREAD. CC 54 and
                                          CC 56 are inert in this mode,
                                          deliberately: the vowel has no
                                          "correct" position to slam to and the
                                          shift's neutral is already the detent,
                                          which `Reset Mid` reaches without
                                          spending a CC (Rule 4). That absence
                                          is a decision, and the simulator's
                                          unread-control check turns it into an
                                          executable claim rather than this
                                          comment. */
{ctl(EQ_S_GC, dest="r0")}
    bl      gfshift
    vmov.f32 s17, s0                   /* the shift multiplier */
{ctl(EQ_S_GB, EQ_S_BTN_B, EQ_FORCE_MAX, dest="r0")}
    bl      ghalf
    vmov.f32 s18, s0                   /* the intensity: 0.0 at and below
                                          centre, 1.0 forced by button B */
{ctl(EQ_S_GA, dest="r0")}
    /* THE VOWEL POSITION. `gftab`'s clamp-split-lerp, on a five-row table
       instead of a seventeen-entry one, and inline rather than a helper because
       everything it produces has to outlive three `bl gsetband` calls anyway. */
    vmov    s0, r0
    vcvt.f32.u32 s0, s0
{_f("s1", "gfmtis", "r1")}
    vmul.f32 s0, s0, s1                /* 0.0 .. 4.0 across the knob */
    vmov.f32 s1, #4.0
    vcmpe.f32 s0, s1
    vmrs    APSR_nzcv, fpscr
    ble     gmfmc
    vmov.f32 s0, s1                    /* THE TOP CLAMP IS NOT DECORATIVE. A
                                          14-bit CC can carry 16383 where this
                                          arm expects at most 16256, which is
                                          4.03 rows into a five-row table -- a
                                          read off the end of it, straight into
                                          whatever the pool put next. `gftab`
                                          clamps for the same reason. The bottom
                                          needs no clamp: an unsigned convert of
                                          a store word cannot go below zero. */
gmfmc:
    vcvt.u32.f32 s1, s0
    vmov    r0, s1
    cmp     r0, #{len(EQ_FMT_TABLE) - 2}
    it      hi
    movhi   r0, #{len(EQ_FMT_TABLE) - 2}
    vmov    s1, r0
    vcvt.f32.u32 s1, s1
    vsub.f32 s16, s0, s1               /* the row in r0, clamped so that row + 1
                                          is still inside the table -- `gftab`'s
                                          own guard on its top entry -- and the
                                          fraction in s16.

                                          s16, s17, s18 AND r8 ARE ALL
                                          CALLEE-SAVED, because every one of
                                          them has to survive THREE `bl
                                          gsetband` calls. This is `gmtilt`'s
                                          and `gmmnot`'s case rather than
                                          `gmphone`'s: each value is consumed
                                          once per band, so there is no
                                          reordering that removes the hazard,
                                          and the prologue's
                                          `vpush {{d8, d9, d10}}` is what
                                          protects them.

                                          `gmphone`'s lesson still applies to
                                          the one value it fits: the per-band
                                          GAIN is loaded last, with no call
                                          whatsoever between it and `gsetband`,
                                          so it never needs protecting at all.
                                          Choose per value, not by rote. */
    add.w   r0, r0, r0, lsl #1         /* row * 3 floats ... */
    adr.w   r8, gfmtf
    add.w   r8, r8, r0, lsl #2         /* ... which is the twelve-byte stride */
    movs    r2, #7
    str     r2, [r6, #{EQ_S_MASK}]     /* WRITTEN BEFORE THE LOOP: three bands
                                          are designed unconditionally, so 7 is
                                          correct from the first instruction and
                                          no early exit added later can leave the
                                          mode half-described */
    movs    r7, #0
gmfmtl:
    add.w   r3, r8, r7, lsl #2
    vldr    s0, [r3]                   /* this vowel's Fn ... */
    vldr    s1, [r3, #12]              /* ... and the next vowel's, one row on */
    vsub.f32 s1, s1, s0
    vfma.f32 s0, s1, s16               /* between the two, by the fraction */
    vmul.f32 s0, s0, s17               /* and shifted -- all three formants move
                                          together, which is what makes it read
                                          as a bigger or smaller mouth rather
                                          than as a different vowel */
    adr.w   r3, gfmtq
    add.w   r3, r3, r7, lsl #2
    vldr    s1, [r3]                   /* this formant's Q: fixed per formant,
                                          not per vowel */
    adr.w   r3, gfmtg
    add.w   r3, r3, r7, lsl #2
    vldr    s2, [r3]
    vmul.f32 s2, s2, s18               /* the gain LAST, and nothing between
                                          here and `gsetband`. All three reach
                                          0 dB together at centre, and rise as a
                                          declining ladder -- F1 loudest,
                                          because F1 is the body of the vowel */
    mov     r0, r7
    movs    r1, #{EQ_T_PARAM}
    bl      gsetband                   /* unconditional, all three times: there
                                          is no branch in this routine that
                                          reaches EQ_S_MASK while skipping a
                                          band */
    adds    r7, #1
    cmp     r7, #3
    blo     gmfmtl
    b.w     gmout

gmout:
    /* EVERY BAND THE MODE DID NOT BUILD IS DESIGNED AS AN EXPLICIT IDENTITY,
       not switched off. This is what makes a mode change silent, and it is
       worth understanding before anyone "simplifies" it back.

       A band that stops being enabled stops contributing INSTANTLY: the output
       jumps from whatever that band was doing to dry, and a jump is a click.
       A band that starts being enabled is worse -- it resumes with whatever
       coefficients and filter history it had when it last ran, which is the
       ring b7 shipped.

       Neither is fixable by clearing state. Clearing state removes the ring and
       keeps the click, which is what b8 did and what the user rejected.

       So no band is ever switched off. Unused bands are designed as a Param at
       0 dB, which 1010music's own designer resolves to b0 = 1, b1 = a1,
       b2 = a2 -- H(z) = 1 exactly, a bit-exact wire once its state settles, and
       self-correcting because z1 accumulates a1*(x - y) which is zero when
       y = x. The band keeps running, so its history stays continuous and there
       is nothing stale to resume from.

       Then the ONLY thing that ever changes is the target coefficient set, and
       the stock smoother already ramps current toward target with a 50 ms time
       constant -- (blockSize/sampleRate)/0.05 per block, set by the ctor at
       obj+0x168. Every transition, in both directions, becomes that ramp.

       Measured, not assumed: across every pair of designs these five modes can
       produce, the interpolation path stays inside R2 AND never becomes more
       resonant than its own endpoints -- 0 excursions in 2970 ordered pairs,
       and 0 again for every design paired with this identity filler. So the
       ramp cannot ring on the way through.

       This also means the arm never writes a raw coefficient. It sets
       parameters and lets 1010music design and smooth, which keeps us out of
       the b0-versus-b2 slot-ordering question entirely. */
    movs    r7, #0
gmfill:
    add.w   r1, r7, r7, lsl #2
    add.w   r1, r4, r1, lsl #2
    ldr.w   r1, [r1, #{EQ_EN_OFF:#x}]
    cmp     r1, #0
    bne     gmfnext                    /* the mode built this one */
    mov     r0, r7
    movs    r1, #{EQ_T_PARAM}
{_f("s0", "gnullf", "r2")}
    vmov.f32 s1, #1.0
    movs    r3, #0
    vmov    s2, r3
    bl      gsetband
gmfnext:
    adds    r7, #1
    cmp     r7, #4
    blo     gmfill
    pop     {{pc}}

    .align 2
gdtab:
    .word 0
    .word {EQ_DESIGNERS[1] | 1:#010x}
    .word {EQ_DESIGNERS[2] | 1:#010x}
    .word {EQ_DESIGNERS[3] | 1:#010x}
    .word {EQ_DESIGNERS[4] | 1:#010x}
    .word {EQ_DESIGNERS[5] | 1:#010x}
gmixt:
    .byte {EQ_T_LSHELF}
    .byte {EQ_T_PARAM}
    .byte {EQ_T_HSHELF}
    .byte 0
    .align 2
gmixf:
{chr(10).join(f"    .float {v!r}" for v in EQ_MIX_F)}
gmixq:
    .float {EQ_Q_FIXED!r}
    .float {EQ_Q_BELL!r}
    .float {EQ_Q_FIXED!r}
gmixk:                                 /* the kill shape, per band */
    .byte {EQ_T_LCUT}                  /* kill low  -> high-pass at 150 Hz */
    .byte {EQ_T_PARAM}                 /* kill mid  -> a wide scoop at 700 Hz */
    .byte {EQ_T_HCUT}                  /* kill high -> low-pass at 4 kHz */
    .byte 0
    .align 2
gmixkq:
    .float {EQ_Q_FIXED!r}
    .float {EQ_Q_BELL!r}
    .float {EQ_Q_FIXED!r}
gmixkg:
    .float 0.0
    .float {EQ_NOTCH_G!r}
    .float 0.0
gqres:   .float {EQ_Q_LO!r}
         .float {EQ_Q_HI - EQ_Q_LO!r}
gqnot:   .float {EQ_QN_LO!r}
         .float {EQ_QN_HI - EQ_QN_LO!r}
gmnsp:   .float {EQ_MN_SP_LO!r}        /* Multi Notch's spread, in gftab table
                                          steps, in gqmap's {{lo, span}} shape.
                                          A fixed step count is a fixed RATIO --
                                          that is the whole trick, and it is why
                                          the stack can never be a harmonic
                                          series. See EQ_MN_SP_LO for where the
                                          octave falls on the knob. */
         .float {EQ_MN_SP_HI - EQ_MN_SP_LO!r}
gqmn:    .float {EQ_MN_Q!r}            /* one Q for all four notches: there is no
                                          fourth encoder, and there should not be */
gqph:    .float 0.5
         .float {EQ_PHONE_HALF!r}
gqfix:   .float {EQ_Q_FIXED!r}
gqbell:  .float {EQ_Q_BELL!r}
ggcut:   .float {EQ_G_CUT!r}
ggboo:   .float {EQ_G_BOOST!r}
gtiltg:  .float {EQ_TILT_G!r}          /* ONE constant, used both ways round --
                                          which is the symmetry, in the pool */
ghsinv:  .float {1.0 / EQ_HALF_SPAN!r}
gccinv:  .float {1.0 / EQ_CC_MAX!r}
gidxs:   .float {float(EQ_TBL_N) / EQ_CC_MAX!r}
gdjspr:  .float {EQ_DJ_SPREAD / float(EQ_TBL_N)!r}
ghspan:  .float {1.0 / (EQ_CC_MAX - EQ_CENTRE)!r}
gbpwid:  .float {EQ_BP_WIDE!r}
gbpnar:  .float {EQ_BP_WIDE - EQ_BP_NARROW!r}
gnotchg: .float {EQ_NOTCH_G!r}
gnullf:  .float {EQ_NULL_FREQ!r}
gfmtis:  .float {float(len(EQ_FMT_TABLE) - 1) / EQ_CC_MAX!r}
                                       /* the vowel knob's scale: raw CC to a
                                          row index 0 .. 4, gidxs's shape with
                                          a different table length */

/* The shift law's two spans, in `ggain`'s own two-constant shape: the
   multiplier is 1.0 + span * u either side of the detent, so ONE `vfma` serves
   both directions and the detent is a true unity rather than something that
   rounds to one. An octave down is a span of -0.5, an octave up is +1.0.

   BOTH LABELS ARE LOAD-BEARING AND THIS COMMENT IS DELIBERATELY ABOVE THEM
   RATHER THAN TRAILING THE FIRST. A block comment opened on a `.float` line and
   closed several lines later swallows every label in between, and that is not a
   hypothetical: it ate `gfmtup` on the first attempt at this pool. The only
   symptom was a symbol quietly missing from the table. */
gfmtdn:  .float {EQ_FMT_DN - 1.0!r}
gfmtup:  .float {EQ_FMT_UP - 1.0!r}
gfmtq:                                 /* per FORMANT, not per vowel: F1 broad,
                                          F2 and F3 progressively narrower */
{chr(10).join(f"    .float {v!r}" for v in EQ_FMT_Q)}
gfmtg:                                 /* the declining gain ladder, in dB at
                                          full intensity. F1 loudest, because it
                                          is the body of the vowel */
{chr(10).join(f"    .float {v!r}" for v in EQ_FMT_G)}
gfmtf:                                 /* FIVE VOWELS x THREE FORMANTS, ROW
                                          MAJOR. A vowel is a twelve-byte
                                          stride, so the NEXT vowel's Fn sits at
                                          [row_ptr, #12] and the interpolation
                                          needs no second pointer.

                                          Frequencies in HERTZ, written straight
                                          into the object -- this is the one
                                          mode whose frequencies do not come out
                                          of `gftab`, so `gftab`'s clamp is not
                                          protecting them. EQ_FMT_TABLE's
                                          import-time assert is what does, at
                                          both extremes of the shift. */
{fmt_tbl}
gtable:
{chr(10).join(f"    .float {v!r}" for v in EQ_FTABLE)}
"""
    code, syms = assemble("hookG", src)
    code = bytearray(code)
    code[syms["gnext"]:syms["gnext"] + 4] = enc_b_bl(org + syms["gnext"], EQ_HANDLER, False)
    return bytes(code)


# Optional Program Change beta. These addresses are specific to stock 2.3.4.
# Use the PresetMgr's committed name, never SessionMgr's requested bank name.
PC_MANAGER = 0x24022578
PC_LIST = PC_MANAGER + 0x32c
PC_PENDING = PC_MANAGER + 0x390
PC_CURRENT = PC_MANAGER + 0x394
PC_STATE = PATCH_RAM + 0x80
PC_VALID = 0x50434e32
assert PC_STATE >= EQ_STORE_END and PC_STATE + 8 <= SRAM_END
PC_BETA_VERSION = "2.3.8-pc1"
PC_CALLS = (
    (0x0806b054, 0x08069ed4, "hregister"),
    (0x080ac35e, 0x080a396c, "hpost"),
    (0x0807f768, 0x0807c258, "hload"),
    # Invalidate before engine teardown as well: a dirty list may no longer
    # resolve an index after LoadBank has stopped the old preset.
    (0x0807c298, 0x080939d0, "hteardown"),
    (0x0807c7e8, 0x080939d0, "hteardown"),
    (0x0808aa0c, 0x080442c4, "hinit"),
    (0x0808bd52, 0x08044328, "hinvalidate"),
    # Rename/Save As assigns the new current name BEFORE filesystem success.
    # Invalidate here; the next completed load will establish identity again.
    (0x0808c460, 0x08044328, "hinvalidate"),
    (0x0807f994, 0x0808b4e8, "hresponse"),
)
PC_MIRROR_VA = 0x080ac766
PC_MIRROR_OLD = bytes.fromhex("003b18bf0123")  # subs #0; it ne; movne #1
PC_MIRROR_NEW = bytes.fromhex("022b88bf0123")  # cmp #2; it hi; movhi #1


def build_program_change_hook(org):
    """H: enum UI, mode transport, and a conservative loaded-name filter.

    All entries replace BLs. Tail calls restore live arguments; the filter
    restores r0-r6 and its entire frame before either LoadBank or the caller.
    No hook runs on the audio thread. See DEV-README's Program Change section.
    """
    src = f"""
.syntax unified
.thumb
.section .text
.global _sH
.thumb_func
_sH:
hregister:
    push    {{r4, lr}}
    sub     sp, #8
    mov     r2, r3                  /* numeric label -> enum label */
    adr.w   r3, hmenu
    movs    r4, #3
    str     r4, [sp]
    ldr     r4, [sp, #24]           /* original caller's XML name at +8 */
    str     r4, [sp, #4]
hregister_call:
    .short  0, 0
    add     sp, #8
    pop     {{r4, pc}}

hpost:
    push    {{r2, r3}}
    ldrb.w  r2, [r5, #0x2b0]
    str     r2, [r1, #0x10]         /* unused word of the 24-byte PC message */
    pop     {{r2, r3}}
hpost_next:
    .short  0, 0

hinit:
    push    {{r2, r3}}
    ldr     r2, =0x{PC_STATE:08x}
    movs    r3, #0
    str     r3, [r2]
    str     r3, [r2, #4]
    pop     {{r2, r3}}
hinit_next:
    .short  0, 0

hinvalidate:
    push    {{r2, r3}}
    ldr     r2, =0x{PC_STATE:08x}
    movs    r3, #0
    str     r3, [r2]
    str     r3, [r2, #4]
    pop     {{r2, r3}}
hinvalidate_next:
    .short  0, 0

hteardown:
    push    {{r2, r3}}
    ldr     r2, =0x{PC_STATE:08x}
    movs    r3, #0
    str     r3, [r2]
    str     r3, [r2, #4]
    pop     {{r2, r3}}
hteardown_next:
    .short  0, 0

hresponse:
    push    {{r4, r5, r6, lr}}
    mov     r4, r1                  /* response buffer survives stock call */
    mov     r5, r0
hresponse_call:
    .short  0, 0
    push    {{r0, r1, r2, r3}}       /* preserve stock results, even scratch */
    cmp     r0, #0
    beq     hresponse_out
    ldr     r1, =0x{PC_MANAGER:08x}
    cmp     r5, r1
    bne     hresponse_out
    ldrb    r1, [r4]
    cmp     r1, #3                  /* actual load completion, any source */
    bne     hresponse_out
    ldr     r1, [r4, #0x10]
    cmp     r1, #0
    beq     hresponse_invalid
    ldr     r1, =0x{PC_VALID:08x}
    b       hresponse_store
hresponse_invalid:
    movs    r1, #0
hresponse_store:
    ldr     r2, =0x{PC_STATE:08x}
    mvn.w   r3, r1
    str     r3, [r2, #4]
    str     r1, [r2]
hresponse_out:
    pop     {{r0, r1, r2, r3}}
    pop     {{r4, r5, r6, pc}}

hload:
    push    {{r0, r1, r2, r3, r4, r5, r6, lr}}
    sub     sp, #256                /* same bounded name buffer as stock */
    ldr     r3, [sp, #0x198]        /* caller +0x78: PC message +0x10 */
    cmp     r3, #2
    bne     hload_stock
    ldr     r3, =0x{PC_STATE:08x}
    ldr     r4, [r3]
    ldr     r5, =0x{PC_VALID:08x}
    cmp     r4, r5
    bne     hload_stock
    ldr     r4, [r3, #4]
    mvn.w   r5, r5
    cmp     r4, r5
    bne     hload_stock
    ldr     r3, =0x{PC_PENDING:08x}
    ldr     r3, [r3]
    ldrb    r3, [r3]
    cmp     r3, #0
    bne     hload_stock             /* let stock handle an outstanding load */
    ldr     r3, =0x{PC_CURRENT:08x}
    ldr     r4, [r3]
    ldrb    r3, [r4]
    cmp     r3, #0
    beq     hload_stock
    ldr     r0, =0x24022978
hload_idle:
    .short  0, 0                   /* stock LoadBank's file-queue guard */
    cmp     r0, #0
    beq     hload_stock
    ldr     r0, =0x{PC_LIST:08x}
    ldr     r1, [sp, #260]          /* original program index */
    mov     r2, sp
hload_resolve:
    .short  0, 0                   /* refresh list if dirty, resolve index */
    cmp     r0, #0
    beq     hload_stock
    mov     r5, sp
    movw    r6, #256
hload_compare:
    ldrb    r0, [r4], #1
    ldrb    r1, [r5], #1
    cmp     r0, r1
    bne     hload_stock
    cmp     r0, #0
    beq     hload_same
    subs    r6, #1
    bne     hload_compare
    /* No terminator: do not suppress a load on uncertain identity. */
hload_stock:
    add     sp, #256
    pop.w   {{r0, r1, r2, r3, r4, r5, r6, lr}}
hload_next:
    .short  0, 0
hload_same:
    add     sp, #256
    pop     {{r0, r1, r2, r3, r4, r5, r6, pc}}
    .balign 4
    .ltorg
hmenu:
    .word   0x080c0030, 0x080c0034
hmenu_new_ptr:
    .word   0
hmenu_new:
    .asciz  "NEW ONLY"
    .balign 4
"""
    code, syms = assemble("hookH", src)
    code = bytearray(code)
    for sym, target, call in (
        ("hregister_call", 0x08069e7c, True),
        ("hpost_next", 0x080a396c, False),
        ("hinit_next", 0x080442c4, False),
        ("hinvalidate_next", 0x08044328, False),
        ("hteardown_next", 0x080939d0, False),
        ("hresponse_call", 0x0808b4e8, True),
        ("hload_idle", 0x08093d30, True),
        ("hload_resolve", 0x0804565c, True),
        ("hload_next", 0x0807c258, False),
    ):
        off = syms[sym]
        code[off:off + 4] = enc_b_bl(org + off, target, call)
    struct.pack_into("<I", code, syms["hmenu_new_ptr"], org + syms["hmenu_new"])
    return bytes(code), syms


def apply_program_change(d):
    """Guard every mutation, append H, then install its call sites."""
    for site, target, _ in PC_CALLS:
        got = bytes(d[site - BASE:site - BASE + 4])
        want = enc_b_bl(site, target, True)
        if got != want:
            refuse(f"The Program Change call at {site:#x} is not what it should be.",
                   f"found {got.hex()}, expected {want.hex()}")
    off = PC_MIRROR_VA - BASE
    if bytes(d[off:off + len(PC_MIRROR_OLD)]) != PC_MIRROR_OLD:
        refuse("The Program Change setting mirror is not what it should be.")
    org = BASE + len(d)
    assert org % 4 == 0
    code, syms = build_program_change_hook(org)
    d += code
    for site, _, entry in PC_CALLS:
        d[site - BASE:site - BASE + 4] = enc_b_bl(site, org + syms[entry], True)
    d[off:off + len(PC_MIRROR_OLD)] = PC_MIRROR_NEW
    vprint(f"H Program Change {len(code)} bytes at {org:#010x}, state {PC_STATE:#x}")


# --- Settings page 2: ProgChange, Clk Loss, SD Mode, Flip UI ---------------
# The Settings screen is built from halfword parameter ids, eight to a page,
# zero meaning an empty slot. Stock page 2 holds three; the patch adds Flip UI
# (id 0x78) and Clk Loss (id 0x13a).
SETTINGS_P2_VA = 0x080c6e7c
SETTINGS_P2_OLD = struct.pack("<8H", 0xc3, 0xb5, 0x17b, 0, 0, 0, 0, 0)

# --- I: Flip UI finished ----------------------------------------------------
# Stock applies `uiflip` to the buttons and knobs only. Both of its handlers
# store !uiflip into the button object's flag; I hooks those stores to send the
# panel MADCTL (0x36) -- 0xC0 flipped, 0x00 normal -- and mirrors touch.
# The apply-all handler runs once the settings file is read, so a saved On
# takes effect about a second after the first screen is drawn.
FLIP_FLAG = 0x24000e54 + 0xc14       # 1 = normal, 0 = flipped
LCD_CMD_FN = 0x08046080              # (cmd, len, buf) over the panel's SPI
LCD_INVON_CALL = 0x080462da          # init's INVON call proves the writer
FLIP_SITES = (                        # (site, stock bytes, entry, return)
    (0x0807c64c, "82f8143c", "iapply_a", 0x0807c650),   # apply-all
    (0x0807d5f2, "82f8143c", "iapply_b", 0x0807d5f6),   # live edit
    (0x0804659e, "9df80830", "itouch", 0x080465a2),     # GT911 point join
)
TOUCH_CLAMP_VA, TOUCH_CLAMP = 0x080465e8, "b2f5a07f"   # cmp.w r2, #0x140


def build_flip_ui_hook(org):
    """I: panel rotation follows Flip UI; touch points mirror with it.

    The two apply stubs replay the displaced flag store, then save r0-r3,
    r12 and LR around the LCD writer. The touch stub runs inside the GT911
    read loop with r1 at the point record; r2 and r3 are dead there.
    """
    src = f"""
.syntax unified
.thumb
.section .text
.global _sI
.thumb_func
_sI:
iapply_a:
    strb.w  r3, [r2, #0xc14]
    push    {{r0, r1, r2, r3, r12, lr}}
    sub     sp, #8
    bl      imadctl
    add     sp, #8
    pop     {{r0, r1, r2, r3, r12, lr}}
iapply_a_next:
    .short  0, 0
iapply_b:
    strb.w  r3, [r2, #0xc14]
    push    {{r0, r1, r2, r3, r12, lr}}
    sub     sp, #8
    bl      imadctl
    add     sp, #8
    pop     {{r0, r1, r2, r3, r12, lr}}
iapply_b_next:
    .short  0, 0
imadctl:                            /* r3 = the flag just stored */
    push    {{r4, lr}}
    sub     sp, #8
    cmp     r3, #0
    ite     eq
    moveq   r3, #0xc0               /* flipped: MY | MX */
    movne   r3, #0
    strb    r3, [sp]
    mov     r2, sp
    movs    r1, #1
    movs    r0, #0x36               /* MADCTL */
imadctl_call:
    .short  0, 0
    add     sp, #8
    pop     {{r4, pc}}
itouch:
    ldr     r2, =0x{FLIP_FLAG:08x}
    ldrb    r2, [r2]
    cbnz    r2, itouch_out
    movw    r2, #319
    ldrh    r3, [r1, #4]
    sub     r3, r2, r3
    strh    r3, [r1, #4]
    movw    r2, #239
    ldrh    r3, [r1, #6]
    sub     r3, r2, r3
    strh    r3, [r1, #6]
itouch_out:
    ldrb.w  r3, [sp, #8]
itouch_next:
    .short  0, 0
    .balign 4
    .ltorg
"""
    code, syms = assemble("hookI", src)
    code = bytearray(code)
    for sym, target, call in (
        ("iapply_a_next", FLIP_SITES[0][3], False),
        ("iapply_b_next", FLIP_SITES[1][3], False),
        ("imadctl_call", LCD_CMD_FN, True),
        ("itouch_next", FLIP_SITES[2][3], False),
    ):
        off = syms[sym]
        code[off:off + 4] = enc_b_bl(org + off, target, call)
    return bytes(code), syms


# --- J: Clk Loss, INTERNAL / HOLD --------------------------------------------
# The clock's live tempo is the float at +0x18; the preset's configured tempo
# (`globtempo`, normally 120) is at +0x13c. Five places load +0x13c when no
# external clock remains: the timeout, two Starts and two locates. HOLD makes
# each use +0x18 instead, once an external clock has been followed. SetTempo
# also skips its rebuild while holding, so a preset load keeps the held tempo
# rather than bringing its own. A returning clock always takes over again.
CLK_ID = 0x13a                       # unregistered in stock, no code refers to it
CLK_MODE = PATCH_RAM + 0x88          # CLK_MAGIC = HOLD, anything else INTERNAL
CLK_HELD = PATCH_RAM + 0x8c          # CLK_MAGIC once an external tempo was used
CLK_MAGIC = 0x484f4c44               # "HOLD"; never zero, unlike stale RAM
assert CLK_MODE >= PC_STATE + 8 and CLK_HELD + 4 <= SRAM_END
CLK_REG_LIMIT_VA, CLK_REG_LIMIT = 0x08069e4e, "b3f5e87f"   # cmp.w r3, #0x1d0
CLK_STORE_CAP_VA, CLK_STORE_CAP = 0x08089e08, "4021"       # movs r1, #0x40
assert CLK_ID <= 0x1d0
CLK_CALLS = (                        # (site, stock target, entry) -- all BLs
    (0x0806abbc, 0x08069ed4, "jreg"),     # uiflip's registration
    (0x08089ef4, 0x0808951e, "jstore"),   # uiflip's global-store entry
    (0x0807c5be, 0x080937f4, "jpost"),    # apply-all posts every setting
    (0x0807d51c, 0x080937f4, "jpost"),    # a live edit posts the setting
)
CLK_SITES = (                        # (site, stock bytes, entry, return)
    (0x080a6258, "c4f83c31", "jctor", 0x080a625c),   # ctor seeds 120.0
    (0x080a6372, "14f80810", "jheld", 0x080a6376),   # per-block source read
    (0x080a6522, "94ed4f0a", "jt1", 0x080a6526),     # timeout fallback
    (0x080a65e6, "90ed4f0a", "jt2", 0x080a65ea),     # Start, internal
    (0x080a6674, "94ed4f0a", "jt3", 0x080a6678),     # Start, CV not locked
    (0x080a6712, "90ed4f0a", "jt4", 0x080a6716),     # locate, internal
    (0x080a67a0, "94ed4f0a", "jt5", 0x080a67a4),     # locate, CV not locked
    (0x080a67d6, "87b02bb1", "jset", None),          # SetTempo: rebuild?
)
CLK_SET_STORE = 0x080a67da           # SetTempo's store-only path
CLK_SET_REBUILD = 0x080a67e6         # SetTempo's rebuild path


def build_clock_hold_hook(org):
    """J: the Clk Loss setting and the HOLD behaviour behind it.

    Register, store and post wrappers replace BLs and tail-call stock with
    the original arguments and stack. The clock stubs run on the audio
    thread and touch only r3, r12, s14, s15 and, when holding, s0 -- all
    dead or about to be consumed at each site (see DEV-README).
    """
    src = f"""
.syntax unified
.thumb
.section .text
.global _sJ
.thumb_func
_sJ:
jreg:                               /* register Clk Loss, then uiflip */
    push    {{r0, r1, r2, r3, r4, lr}}
    sub     sp, #8
    movw    r1, #0x{CLK_ID:x}
    adr.w   r2, jlabel
    adr.w   r3, jvalues
    movs    r4, #2
    str     r4, [sp]
    adr.w   r4, jxml
    str     r4, [sp, #4]
jreg_call:
    .short  0, 0                    /* enum registrar */
    add     sp, #8
    pop     {{r0, r1, r2, r3, r4, lr}}
jreg_next:
    .short  0, 0                    /* the original numeric registration */

jstore:                             /* uiflip's entry, then Clk Loss = 0 */
    push    {{r0, r1, r2, lr}}
jstore_call:
    .short  0, 0
    pop     {{r0, r1, r2, lr}}
    movw    r1, #0x{CLK_ID:x}
    movs    r2, #0
jstore_next:
    .short  0, 0

jpost:                              /* catch Clk Loss; post everything else */
    movw    r12, #0x{CLK_ID:x}
    cmp     r2, r12
    bne     jpost_next
    ldr     r12, =0x{CLK_MODE:08x}
    cbz     r3, jpost_internal
    ldr     r3, =0x{CLK_MAGIC:08x}
    str     r3, [r12]
    bx      lr
jpost_internal:
    str     r3, [r12]               /* INTERNAL */
    str     r3, [r12, #4]           /* and forget any held tempo */
    bx      lr
jpost_next:
    .short  0, 0

jctor:                              /* clock object constructed at boot */
    str.w   r3, [r4, #0x13c]
    ldr     r3, =0x{CLK_MODE:08x}
    mov.w   r12, #0
    str     r12, [r3]
    str     r12, [r3, #4]
jctor_next:
    .short  0, 0

jheld:                              /* every block: is a clock in charge? */
    ldrb.w  r1, [r4, r8]
    cbz     r1, jheld_next
    ldr     r12, =0x{CLK_HELD:08x}
    ldr     r3, =0x{CLK_MAGIC:08x}
    str     r3, [r12]
jheld_next:
    .short  0, 0

jt1:
    vldr    s0, [r4, #0x13c]
    vldr    s15, [r4, #0x18]
    bl      jhold
jt1_next:
    .short  0, 0
jt2:
    vldr    s0, [r0, #0x13c]
    vldr    s15, [r0, #0x18]
    bl      jhold
jt2_next:
    .short  0, 0
jt3:
    vldr    s0, [r4, #0x13c]
    vldr    s15, [r4, #0x18]
    bl      jhold
jt3_next:
    .short  0, 0
jt4:
    vldr    s0, [r0, #0x13c]
    vldr    s15, [r0, #0x18]
    bl      jhold
jt4_next:
    .short  0, 0
jt5:
    vldr    s0, [r4, #0x13c]
    vldr    s15, [r4, #0x18]
    bl      jhold
jt5_next:
    .short  0, 0

jhold:                              /* s0 configured, s15 live -> s0 */
    ldr     r12, =0x{CLK_MODE:08x}
    ldr     r3, [r12]
    ldr     r12, =0x{CLK_MAGIC:08x}
    cmp     r3, r12
    bne     jhold_out
    ldr     r12, =0x{CLK_HELD:08x}
    ldr     r3, [r12]
    ldr     r12, =0x{CLK_MAGIC:08x}
    cmp     r3, r12
    bne     jhold_out
    vldr    s14, jmin
    vcmp.f32 s15, s14
    vmrs    APSR_nzcv, fpscr
    ble     jhold_out               /* also NaN */
    vldr    s14, jmax
    vcmp.f32 s15, s14
    vmrs    APSR_nzcv, fpscr
    bge     jhold_out
    vmov.f32 s0, s15
jhold_out:
    bx      lr

jset:                               /* r3 = source; r4 = clock; s0 = tempo */
    sub     sp, #0x1c
    cbnz    r3, jset_store          /* stock: a clock is in charge */
    ldr     r12, =0x{CLK_MODE:08x}
    ldr     r3, [r12]
    ldr     r12, =0x{CLK_MAGIC:08x}
    cmp     r3, r12
    bne     jset_rebuild
    ldr     r12, =0x{CLK_HELD:08x}
    ldr     r3, [r12]
    ldr     r12, =0x{CLK_MAGIC:08x}
    cmp     r3, r12
    bne     jset_rebuild
    vldr    s15, [r4, #0x18]
    vldr    s14, jmin
    vcmp.f32 s15, s14
    vmrs    APSR_nzcv, fpscr
    ble     jset_rebuild
    vldr    s14, jmax
    vcmp.f32 s15, s14
    vmrs    APSR_nzcv, fpscr
    bge     jset_rebuild
jset_store:
    .short  0, 0                    /* holding: record the preset's tempo only */
jset_rebuild:
    .short  0, 0

    .balign 4
jmin:
    .float  20.0
jmax:
    .float  1000.0
    .ltorg
jvalues:
    .word   0, 0                    /* -> jinternal, jholdtxt */
    /* Every string label is word aligned. parse_elf32 clears bit 0 of each
       symbol (right for Thumb code), so a string at an odd offset would be
       pointed at one byte early -- "HOLD" read as the NUL ending "INTERNAL". */
    .balign 4
jlabel:
    .asciz  "Clk Loss:"
    .balign 4
jinternal:
    .asciz  "INTERNAL"
    .balign 4
jholdtxt:
    .asciz  "HOLD"
    .balign 4
jxml:
    .asciz  "clklossmode"
    .balign 4
"""
    code, syms = assemble("hookJ", src)
    code = bytearray(code)
    for sym, target, call in (
        ("jreg_call", 0x08069e7c, True),
        ("jreg_next", 0x08069ed4, False),
        ("jstore_call", 0x0808951e, True),
        ("jstore_next", 0x0808951e, False),
        ("jpost_next", 0x080937f4, False),
        ("jctor_next", CLK_SITES[0][3], False),
        ("jheld_next", CLK_SITES[1][3], False),
        ("jt1_next", CLK_SITES[2][3], False),
        ("jt2_next", CLK_SITES[3][3], False),
        ("jt3_next", CLK_SITES[4][3], False),
        ("jt4_next", CLK_SITES[5][3], False),
        ("jt5_next", CLK_SITES[6][3], False),
        ("jset_store", CLK_SET_STORE, False),
        ("jset_rebuild", CLK_SET_REBUILD, False),
    ):
        off = syms[sym]
        code[off:off + 4] = enc_b_bl(org + off, target, call)
    struct.pack_into("<II", code, syms["jvalues"],
                     org + syms["jinternal"], org + syms["jholdtxt"])
    return bytes(code), syms


def _append(d, code):
    org = BASE + len(d)
    assert org % 4 == 0
    if len(code) % 4:
        code += b"\x00" * (4 - len(code) % 4)
    d += code
    return org


def _guard(d, va, want_hex, what):
    got = bytes(d[va - BASE:va - BASE + len(want_hex) // 2])
    if got.hex() != want_hex:
        refuse(f"The {what} at {va:#x} is not what it should be.",
               f"found {got.hex()}, expected {want_hex}")


def apply_settings_page(d, rows=(0xc3, 0xb5, CLK_ID, 0x17b, 0x78)):
    """Write Settings page 2's rows (8 slots, zero = empty). Guarded, data only.
    The default rows list Clk Loss and Flip UI; opt-in builds add their own."""
    assert len(rows) <= 8
    off = SETTINGS_P2_VA - BASE
    if bytes(d[off:off + 16]) != SETTINGS_P2_OLD:
        refuse("The Settings screen's second page is not what it should be.")
    d[off:off + 16] = struct.pack("<8H", *rows, *([0] * (8 - len(rows))))


def branch_target(d, at):
    """Destination of the Thumb-2 b.w or bl at `at` (T4 / BL encodings)."""
    hw1, hw2 = struct.unpack_from("<HH", d, at - BASE)
    assert hw1 & 0xf800 == 0xf000 and hw2 & 0x9000 == 0x9000, f"no b.w/bl at {at:#x}"
    s = (hw1 >> 10) & 1
    i1 = 1 - (((hw2 >> 13) & 1) ^ s)
    i2 = 1 - (((hw2 >> 11) & 1) ^ s)
    imm = (s << 24) | (i1 << 23) | (i2 << 22) | ((hw1 & 0x3ff) << 12) | ((hw2 & 0x7ff) << 1)
    if s:
        imm -= 1 << 25
    return at + 4 + imm


def apply_flip_ui(d):
    """Guard I's sites and the code they rely on, append I, install it."""
    if bytes(d[LCD_INVON_CALL - BASE:LCD_INVON_CALL - BASE + 4]) != \
            enc_b_bl(LCD_INVON_CALL, LCD_CMD_FN, True):
        refuse(f"The panel set-up call at {LCD_INVON_CALL:#x} is not what it should be.")
    _guard(d, TOUCH_CLAMP_VA, TOUCH_CLAMP, "touch clamp")
    for site, stock, _, _ in FLIP_SITES:
        _guard(d, site, stock, "Flip UI site")
    code, syms = build_flip_ui_hook(BASE + len(d))
    org = _append(d, bytearray(code))
    for site, _, entry, _ in FLIP_SITES:
        d[site - BASE:site - BASE + 4] = enc_b_bl(site, org + syms[entry], False)
    vprint(f"I Flip UI {len(code)} bytes at {org:#010x}")


def apply_clock_hold(d):
    """Guard J's sites and the limits it relies on, append J, install it."""
    _guard(d, CLK_REG_LIMIT_VA, CLK_REG_LIMIT, "parameter registry limit")
    _guard(d, CLK_STORE_CAP_VA, CLK_STORE_CAP, "global settings capacity")
    for site, target, _ in CLK_CALLS:
        if bytes(d[site - BASE:site - BASE + 4]) != enc_b_bl(site, target, True):
            refuse(f"The Clk Loss call at {site:#x} is not what it should be.")
    for site, stock, _, _ in CLK_SITES:
        _guard(d, site, stock, "clock site")
    code, syms = build_clock_hold_hook(BASE + len(d))
    org = _append(d, bytearray(code))
    for site, _, entry in CLK_CALLS:
        d[site - BASE:site - BASE + 4] = enc_b_bl(site, org + syms[entry], True)
    for site, _, entry, _ in CLK_SITES:
        d[site - BASE:site - BASE + 4] = enc_b_bl(site, org + syms[entry], False)
    vprint(f"J Clk Loss {len(code)} bytes at {org:#010x}, "
           f"mode {CLK_MODE:#x}, held {CLK_HELD:#x}, id {CLK_ID:#x}")
    return org, syms


# --- K: CPU meter (opt-in test build) ---------------------------------------
# The engine already profiles every block: profiler B brackets the per-block
# function 0x08093e48 and records {period, busy} cycle counts. The summariser at
# the top of each block resets the count, so the previous block's pair is always
# entry 0. K reads it after the start call, keeps a smoothed per-mille load and
# a held peak, and the UI's every-tick present call draws a bar into all four
# LTDC buffers (two ARGB1555 layers x double buffer, 240x320 portrait).
# CPU Test (id 0x125, a cycle burner for calibrating the meter) was REMOVED in
# 2.3.9-chr9: on hardware a value of 56 overloaded the module and locked it up,
# and as a SAVED setting it came back at every boot. 0x125 is no longer
# registered or in the global store, so a stale `cputest` in settings.tml has
# no parameter to land on (tests/test_firmware_osd.py follows that path), and
# nothing reads K_BURN any more. Test build only; never in a release as-is.
CPU_METER_VERSION = "2.3.9-cpu2"
CPU_ID_METER = 0x13b                 # enum OFF/ON; unregistered in stock
CPU_ID_TEST_RETIRED = 0x125          # was CPU Test; deliberately left unregistered
K_MODE = PATCH_RAM + 0x90            # CLK_MAGIC = meter on
K_BURN = PATCH_RAM + 0x94            # retired with CPU Test; nothing reads it
K_AVG = PATCH_RAM + 0x98             # per-mille, smoothed
K_PEAK = PATCH_RAM + 0x9c            # per-mille, held then decaying
K_HOLD = PATCH_RAM + 0xa0            # blocks left in the peak hold
assert K_MODE >= CLK_HELD + 4 and K_HOLD + 4 <= SRAM_END
DISPLAY_OBJ = 0x24002e7c
K_CALLS = (                          # (site, stock target, entry) -- all BLs
    (0x08093e72, 0x08044eb8, "kstart"),    # profiler B start, every block
    (0x0805bbb4, 0x08042900, "kdraw"),     # UI present, every UI tick
    (0x08089efe, 0x0808951e, "kstore"),    # global store: the 0xdc entry
)


def build_cpu_meter_hook(org, post_next=0x080937f4, draw_next=0x08042900,
                         start_next=0x08044eb8):
    src = f"""
.syntax unified
.thumb
.section .text
.global _sK
.thumb_func
_sK:
kreg:                               /* chained after jreg; r0 = registry */
    push    {{r0, r1, r2, r3, r4, lr}}
    sub     sp, #16
    movw    r1, #0x{CPU_ID_METER:x}
    adr.w   r2, klabel_meter
    adr.w   r3, kvalues
    movs    r4, #2
    str     r4, [sp]
    adr.w   r4, kxml_meter
    str     r4, [sp, #4]
kreg_call1:
    .short  0, 0                    /* enum registrar */
    add     sp, #16
    pop     {{r0, r1, r2, r3, r4, lr}}
kreg_next:
    .short  0, 0                    /* the original uiflip registration */

kstore:                             /* the 0xdc entry, then ours */
    push    {{r0, r1, r2, lr}}
kstore_call:
    .short  0, 0
    pop     {{r0, r1, r2, lr}}
    movw    r1, #0x{CPU_ID_METER:x}
    movs    r2, #0
kstore_next:
    .short  0, 0

kpost:                              /* chained after jpost */
    movw    r12, #0x{CPU_ID_METER:x}
    cmp     r2, r12
    beq     kpost_meter
kpost_next:
    .short  0, 0
kpost_meter:
    ldr     r12, =0x{K_MODE:08x}
    cbz     r3, kpost_store
    ldr     r3, =0x{CLK_MAGIC:08x}
kpost_store:
    str     r3, [r12]
    bx      lr

kstart:                             /* r0 = profiler B; keep r0-r2, r12 */
    push    {{r0, r1, r2, r4, r12, lr}}
    mov     r4, r0
kstart_call:
    .short  0, 0                    /* start timing this block */
    ldr     r0, [r4]                /* ring: entry 0 = previous block */
    ldr     r1, [r0]                /* period */
    ldr     r2, [r0, #4]            /* busy */
    cbz     r1, kdone
    cmp     r2, r1
    it      hi
    movhi   r2, r1
    movw    r3, #1000
    mul     r2, r2, r3
    udiv    r2, r2, r1              /* per-mille load */
    movw    r12, #1000
    cmp     r2, r12
    it      hi
    movhi   r2, r12
    ldr     r0, =0x{K_AVG:08x}
    ldr     r1, [r0]
    cmp     r1, r12
    it      hi
    movhi   r1, r2                  /* stale RAM: start from now */
    subs    r3, r2, r1
    add     r1, r1, r3, asr #3
    str     r1, [r0]
    ldr     r1, [r0, #4]            /* peak */
    ldr     r3, [r0, #8]            /* hold */
    cmp     r1, r12
    itt     hi
    movhi   r1, #0
    movhi   r3, #0
    cmp     r2, r1
    bls     kpeak_old
    mov     r1, r2
    movw    r3, #375                /* about a second of blocks */
    b       kpeak_save
kpeak_old:
    cbz     r3, kpeak_decay
    subs    r3, #1
    b       kpeak_save
kpeak_decay:
    cmp     r1, #2
    ite     hs
    subhs   r1, #2
    movlo   r1, #0
kpeak_save:
    str     r1, [r0, #4]
    str     r3, [r0, #8]
kdone:
    pop     {{r0, r1, r2, r4, r12, pc}}

kdraw:                              /* r0 = display object, every UI tick */
    push    {{r0, r1, r2, r3, r4, r5, r6, r7, r8, r12, lr}}
    ldr     r1, =0x{K_MODE:08x}
    ldr     r1, [r1]
    ldr     r2, =0x{CLK_MAGIC:08x}
    cmp     r1, r2
    bne     kdraw_out
    mov     r8, r0
    ldr     r1, =0x{K_AVG:08x}
    ldr     r4, [r1]
    ldr     r5, [r1, #4]
    movw    r12, #1000
    cmp     r4, r12
    it      hi
    movhi   r4, r12
    cmp     r5, r12
    it      hi
    movhi   r5, r12
    lsls    r4, r4, #5              /* x 320 / 1000 = x 32 / 100 */
    lsls    r5, r5, #5
    movs    r6, #100
    udiv    r4, r4, r6              /* bar length out of 320 px: full width */
    udiv    r5, r5, r6
    cmp     r5, #0
    it      eq
    movweq  r5, #0xffff             /* no peak tick at zero */
    add.w   r7, r8, #0x120
    bl      kbar
    add.w   r7, r8, #0x12c
    bl      kbar
    add.w   r7, r8, #0x138
    bl      kbar
    add.w   r7, r8, #0x144
    bl      kbar
    dsb
kdraw_out:
    pop     {{r0, r1, r2, r3, r4, r5, r6, r7, r8, r12, lr}}
kdraw_next:
    .short  0, 0                    /* the stock present */

kbar:                               /* r7 = bitmap, r4 level, r5 peak */
    ldr     r0, [r7]
    cbz     r0, kbar_ret
    ldrh    r1, [r7, #4]
    lsls    r1, r1, #1              /* stride in bytes */
    movs    r2, #0
kbar_loop:
    cmp     r2, r5
    beq     kc_peak
    cmp     r2, r4
    bhs     kc_bg
    cmp     r2, #288                /* 90% of 320 */
    bhs     kc_red
    cmp     r2, #224                /* 70% of 320 */
    bhs     kc_amber
    movw    r3, #0x83e0             /* ARGB1555 green */
    b       kc_put
kc_peak:
    movw    r3, #0xffff
    b       kc_put
kc_bg:
    movw    r3, #0x8842
    b       kc_put
kc_red:
    movw    r3, #0xfc00
    b       kc_put
kc_amber:
    movw    r3, #0xfe80
kc_put:
    mul     r6, r2, r1
    add     r6, r6, r0
    strh    r3, [r6]
    strh    r3, [r6, #2]
    strh    r3, [r6, #4]
    strh    r3, [r6, #6]
    ldr     r12, =0xe000ef68        /* DCCMVAC: clean the line to RAM */
    str     r6, [r12]
    adds    r2, #1
    cmp     r2, #320
    blo     kbar_loop
kbar_ret:
    bx      lr

    .balign 4
    .ltorg
kvalues:
    .word   0x080c0030, 0x080c0034  /* stock OFF / ON strings */
    .balign 4
klabel_meter:
    .asciz  "CPU Meter:"
    .balign 4
kxml_meter:
    .asciz  "cpumeter"
    .balign 4
"""
    code, syms = assemble("hookK", src)
    code = bytearray(code)
    for sym, target, call in (
        ("kreg_call1", 0x08069e7c, True),
        ("kreg_next", 0x08069ed4, False),
        ("kstore_call", 0x0808951e, True),
        ("kstore_next", 0x0808951e, False),
        ("kpost_next", post_next, False),
        ("kstart_call", start_next, True),
        ("kdraw_next", draw_next, False),
    ):
        off = syms[sym]
        code[off:off + 4] = enc_b_bl(org + off, target, call)
    return bytes(code), syms


# --- M: clock diagnostic strips (debug build only, never shipped) ------------
# Draws, each UI tick, into all four LTDC buffers at buffer columns 8..11 and
# 14..17 (just above the CPU meter's 0..3): the live tempo +0x18 at 1 px per
# BPM, coloured by the clock source (grey internal, green MIDI, blue CV), the
# fallback +0x13c in white, and a red tick at x = 316..319 while an edit is
# parked in L_PEND. Requires L (for L_CLOCK).
def build_clock_debug_hook(org, draw_next, ctor_next=0x080a625c):
    src = f"""
.syntax unified
.thumb
.section .text
.global _sM
.thumb_func
_sM:
mdraw:                              /* r0 = display object */
    push    {{r0, r1, r2, r3, r4, r5, r6, r7, r8, r12, lr}}
    vpush   {{d7}}
    mov     r8, r0
    ldr     r4, =0x{L_CLOCK:08x}
    ldr     r4, [r4]
    ldr     r1, =0x24000000
    cmp     r4, r1
    blo     mdraw_out
    ldr     r1, =0x2407c000
    cmp     r4, r1
    bhs     mdraw_out
    movw    r1, #0x2c60
    ldrb    r1, [r4, r1]            /* source */
    movw    r5, #0xc210             /* grey: internal */
    cmp     r1, #1
    it      eq
    movweq  r5, #0x801f             /* blue: CV pulse */
    cmp     r1, #2
    it      eq
    movweq  r5, #0x83e0             /* green: MIDI */
    vldr    s15, [r4, #0x18]
    bl      mlen
    mov     r6, r0                  /* live length */
    vldr    s15, [r4, #0x13c]
    bl      mlen
    mov     r7, r0                  /* fallback length */
    ldr     r1, =0x{L_PEND:08x}
    ldr     r1, [r1]
    lsrs    r1, r1, #16
    movw    r2, #0x{L_PEND_MAGIC:x}
    subs    r1, r1, r2              /* 0 if an edit is parked */
    add.w   r2, r8, #0x120
    bl      mbuf
    add.w   r2, r8, #0x12c
    bl      mbuf
    add.w   r2, r8, #0x138
    bl      mbuf
    add.w   r2, r8, #0x144
    bl      mbuf
    dsb
mdraw_out:
    vpop    {{d7}}
    pop     {{r0, r1, r2, r3, r4, r5, r6, r7, r8, r12, lr}}
mdraw_next:
    .short  0, 0

mlen:                               /* s15 BPM -> r0 in 0..319 (NaN -> 0) */
    vcvt.s32.f32 s15, s15
    vmov    r0, s15
    cmp     r0, #0
    it      lt
    movlt   r0, #0
    movw    r3, #319
    cmp     r0, r3
    it      gt
    movgt   r0, r3
    bx      lr

mbuf:                               /* r2 = bitmap; r5 colour, r6/r7 lengths, r1 parked flag */
    push    {{r1, r4, lr}}
    ldr     r4, [r2]
    cbz     r4, mbuf_ret
    ldrh    r12, [r2, #4]
    lsl     r12, r12, #1            /* stride in bytes */
    movs    r0, #0                  /* row = BPM */
mbuf_row:
    mul     r3, r0, r12
    add     r3, r3, r4
    movw    r2, #0x8000             /* black (opaque) */
    cmp     r0, r6
    it      lo
    movlo   r2, r5
    strh    r2, [r3, #16]
    strh    r2, [r3, #18]
    strh    r2, [r3, #20]
    strh    r2, [r3, #22]
    movw    r2, #0x8000
    cmp     r0, r7
    it      lo
    movwlo  r2, #0xffff
    strh    r2, [r3, #28]
    strh    r2, [r3, #30]
    strh    r2, [r3, #32]
    strh    r2, [r3, #34]
    ldr     r1, [sp]
    cbnz    r1, mbuf_nopend
    cmp     r0, #316
    blo     mbuf_nopend
    movw    r2, #0xfc00             /* red: an edit is parked */
    strh    r2, [r3, #16]
    strh    r2, [r3, #28]
mbuf_nopend:
    ldr     r2, =0xe000ef68
    str     r3, [r2]
    adds    r0, #1
    cmp     r0, #320
    blo     mbuf_row
mbuf_ret:
    pop     {{r1, r4, pc}}

mctor:                              /* only when L is absent; r4 = clock */
    ldr     r12, =0x{L_CLOCK:08x}
    str     r4, [r12]
mctor_next:
    .short  0, 0
    .balign 4
    .ltorg
"""
    code, syms = assemble("hookM", src)
    code = bytearray(code)
    for sym, target in (("mdraw_next", draw_next), ("mctor_next", ctor_next)):
        off = syms[sym]
        code[off:off + 4] = enc_b_bl(org + off, target, False)
    return bytes(code), syms


def apply_clock_debug(d, chained, j_org, j_syms, own_ctor):
    site = 0x0805bbb4
    if branch_target(d, site) not in (0x08042900, *chained):
        refuse(f"The UI present call at {site:#x} is not what it should be.")
    jc = j_org + j_syms["jctor_next"]
    code, syms = build_clock_debug_hook(BASE + len(d), branch_target(d, site),
                                        branch_target(d, jc))
    org = _append(d, bytearray(code))
    d[site - BASE:site - BASE + 4] = enc_b_bl(site, org + syms["mdraw"], True)
    apply_clock_debug.entry = org + syms["mdraw"]
    if own_ctor:                       # without L, M records the clock itself
        if branch_target(d, jc) != 0x080a625c:
            refuse("Hook J's jctor_next is not what it should be.")
        d[jc - BASE:jc - BASE + 4] = enc_b_bl(jc, org + syms["mctor"], False)
    vprint(f"M clock debug {len(code)} bytes at {org:#010x}")


def apply_cpu_meter(d, j_org, j_syms, chained=(), row=True):
    """Guard K's sites, append K, install it, and chain J's register and post
    tails through it. A site or tail may already lead to another opt-in hook
    (`chained`), in which case K slots in front of it.

    row=False (rc1): the bar only. K's registration, store entry and post
    wrapper are left out of every chain -- 0x13b is never registered, never
    in the store, never posted -- and the OSD row's post sets K_MODE. The blob
    is K's own, unchanged; its kreg/kstore/kpost are unreachable."""
    calls = [c for c in K_CALLS if row or c[2] != "kstore"]
    for site, target, _ in calls:
        if branch_target(d, site) not in (target, *chained):
            refuse(f"The CPU meter call at {site:#x} is not what it should be.")
    for sym, stock in (("jreg_next", 0x08069ed4), ("jpost_next", 0x080937f4)):
        if branch_target(d, j_org + j_syms[sym]) not in (stock, *chained):
            refuse(f"Hook J's {sym} is not what it should be.")
    code, syms = build_cpu_meter_hook(
        BASE + len(d),
        post_next=branch_target(d, j_org + j_syms["jpost_next"]),
        draw_next=branch_target(d, 0x0805bbb4),
        start_next=branch_target(d, 0x08093e72))
    org = _append(d, bytearray(code))
    for site, _, entry in calls:
        d[site - BASE:site - BASE + 4] = enc_b_bl(site, org + syms[entry], True)
    for sym, entry in (("jreg_next", "kreg"), ("jpost_next", "kpost")) if row else ():
        at = j_org + j_syms[sym]
        d[at - BASE:at - BASE + 4] = enc_b_bl(at, org + syms[entry], False)
    vprint(f"K CPU meter {len(code)} bytes at {org:#010x}, ram {K_MODE:#x}")


# --- L: Preset BPM on Settings page 2, with HOLD capture (opt-in) -------------
# `globtempo` (0x66, registered as BPM: 40-250) lives in the preset's song store
# at SessionMgr+0x2278, not the global store the Settings screen reads. L points
# the Settings screen's reads and writes of 0x66 at the song store, posts edits
# to the engine at the song's padKey 0x10400 exactly as a preset load does (the
# clock then echoes the tempo back via message 0x6f), and relabels the row.
# A user edit releases a HOLD. While Clk Loss is holding with no clock, each UI
# tick copies the held tempo into the song store, so saving the preset keeps it.
PRESET_BPM_VERSION = "2.3.9-bpm7"
# The UI's SessionMgr, whose stores the Settings screen reads: every caller of
# the page builder 0x080776e4 passes this literal. NOT 0x24022978, which is the
# engine-command manager PostSetParam takes -- bpm1 used that and read nothing.
SESSION_MGR = 0x24013cb0
SESSION_MGR_GUARDS = (0x0806d6e0, 0x0806dc14, 0x080921e8)   # literals holding it
SONG_STORE = SESSION_MGR + 0x2278
GLOBAL_STORE = SESSION_MGR + 0x7fd0
SESSION_DIRTY = SESSION_MGR + 0x8004    # 1 = unsaved changes; stock edits set it, save clears it
L_CLOCK = PATCH_RAM + 0xa4           # the clock object, recorded by its ctor
L_PEND = PATCH_RAM + 0xa8            # 0xB9D0_0000 | BPM: an edit made under a clock
L_PEND_MAGIC = 0xb9d0
assert L_CLOCK >= K_HOLD + 4 and L_PEND + 4 <= SRAM_END
L_CALLS = (                          # (site, stock target, entry) -- all BLs
    (0x0806a8c2, 0x08069ed4, "lreg"),      # BPM:'s registration
    (0x0805bbb4, 0x08042900, "ltick"),     # UI present, every UI tick
    (0x08093e72, 0x08044eb8, "lblock"),    # engine block start (profiler B)
)
# bpm3 on hardware: with a clock playing, turning Preset BPM audibly fought
# the clock -- the posted 0x66 reaches the voices' tempo as well as SetTempo's
# store-only fallback, and the clock re-asserts on its next update. So under a
# clock, lpost does NOT post: it parks the BPM in L_PEND, and the next engine
# block writes it straight into the clock's fallback field +0x13c, which is
# exactly what stock SetTempo does when a clock is in charge.
# GetParam / SetParam themselves: redirect (global store, 0x66) to the song
# store at the source. bpm2 hooked three call sites and missed two more -- the
# knob's current-value read 0x080774d0 and the row refresh 0x08076d8c -- so the
# row could be read but never changed. Every other (store, id) passes through.
L_ENTRIES = (                        # (entry, stock bytes, hook, resume)
    (0x080894d0, "70b44589", "lgetall", 0x080894d4),   # push {r4-r6}; ldrh r5
    (0x080894fa, "f0b44689", "lsetall", 0x080894fe),   # push {r4-r7}; ldrh r6
)


def build_preset_bpm_hook(org, post_next, tick_next, ctor_next, block_next=0x08044eb8):
    src = f"""
.syntax unified
.thumb
.section .text
.global _sL
.thumb_func
_sL:
lreg:                               /* register 0x66 under our own label */
    adr.w   r3, llabel
lreg_next:
    .short  0, 0

/* REGISTER CONTRACT. 1010's compiler tracks which registers each callee
   really writes and lets callers keep values in the rest across the call.
   GetParam writes only r0/r2/r3, SetParam only r0/r3, profiler start only r3.
   bpm1-4 used r12 here and r1/r2/r12 in lblock: MIDI clock never locked. */
lgetall:                            /* GetParam's entry: (store, id) */
    cmp     r1, #0x66
    bne     lgetall_go
    ldr     r3, =0x{GLOBAL_STORE:08x}
    cmp     r0, r3
    it      eq
    ldreq   r0, =0x{SONG_STORE:08x}
lgetall_go:
    push    {{r4, r5, r6}}           /* the two displaced instructions */
    ldrh    r5, [r0, #0xa]
lgetall_next:
    .short  0, 0

lsetall:                            /* SetParam's entry: (store, id, value) */
    cmp     r1, #0x66
    bne     lsetall_go
    ldr     r3, =0x{GLOBAL_STORE:08x}
    cmp     r0, r3
    it      eq
    ldreq   r0, =0x{SONG_STORE:08x}
lsetall_go:
    push    {{r4, r5, r6, r7}}
    ldrh    r6, [r0, #0xa]
lsetall_next:
    .short  0, 0

lpost:                              /* chained after jpost */
    cmp     r2, #0x66
    bne     lpost_next
    push    {{r4, r5}}
    ldr     r4, =0x{CLK_HELD:08x}
    str     r4, [r4]                /* not the magic: the edit ends any hold */
    ldr     r4, =0x{L_CLOCK:08x}
    ldr     r4, [r4]
    ldr     r5, =0x24000000
    cmp     r4, r5
    blo     lpost_now
    ldr     r5, =0x2407c000
    cmp     r4, r5
    bhs     lpost_now
    movw    r5, #0x2c60
    ldrb    r5, [r4, r5]
    cbz     r5, lpost_now           /* no clock: post, so it applies at once */
    ldr     r4, =0x{L_PEND:08x}     /* a clock is in charge: park it */
    uxth    r5, r3
    movt    r5, #0x{L_PEND_MAGIC:x}    /* tag the top half */
    str     r5, [r4]
    pop     {{r4, r5}}
    bx      lr                      /* not posted: the clock is undisturbed */
lpost_now:
    ldr     r4, =0x{L_PEND:08x}
    movs    r5, #0
    str     r5, [r4]
    pop     {{r4, r5}}
    ldr     r1, =0x00010400         /* the song's padKey, as a load posts it */
lpost_next:
    .short  0, 0

lblock:                             /* engine block start; r0 = profiler B */
    push    {{r1, r2, r4, lr}}
    vpush   {{d7}}
    ldr     r4, =0x{L_PEND:08x}
    ldr     r1, [r4]
    lsrs    r2, r1, #16
    movw    r3, #0x{L_PEND_MAGIC:x}
    cmp     r2, r3
    bne     lblock_done
    ldr     r2, =0x{L_CLOCK:08x}
    ldr     r2, [r2]
    ldr     r3, =0x24000000
    cmp     r2, r3
    blo     lblock_done
    ldr     r3, =0x2407c000
    cmp     r2, r3
    bhs     lblock_done
    uxth    r1, r1
    vmov    s15, r1
    vcvt.f32.u32 s15, s15
    vstr    s15, [r2, #0x13c]       /* the fallback, as stock SetTempo does */
    movs    r1, #0
    str     r1, [r4]
lblock_done:
    vpop    {{d7}}
    pop     {{r1, r2, r4, lr}}
lblock_next:
    .short  0, 0

lctor:                              /* chained after jctor; r4 = clock */
    ldr     r12, =0x{L_CLOCK:08x}
    str     r4, [r12]
lctor_next:
    .short  0, 0

ltick:                              /* r0 = display object, every UI tick */
    push    {{r0, r1, r2, r3, r4, r5, r6, r7, r8, r12, lr}}
    vpush   {{d7}}
    ldr     r1, =0x{CLK_MODE:08x}
    ldr     r1, [r1]
    ldr     r2, =0x{CLK_MAGIC:08x}
    cmp     r1, r2
    bne     ltick_out
    ldr     r1, =0x{CLK_HELD:08x}
    ldr     r1, [r1]
    cmp     r1, r2
    bne     ltick_out
    ldr     r4, =0x{L_CLOCK:08x}
    ldr     r4, [r4]
    ldr     r1, =0x24000000
    cmp     r4, r1
    blo     ltick_out
    ldr     r1, =0x2407c000
    cmp     r4, r1
    bhs     ltick_out
    movw    r1, #0x2c60
    ldrb    r1, [r4, r1]
    cbnz    r1, ltick_out           /* a clock is in charge: nothing held */
    vldr    s15, [r4, #0x18]
    vldr    s14, lmin
    vcmp.f32 s15, s14
    vmrs    APSR_nzcv, fpscr
    ble     ltick_out
    vldr    s14, lmax
    vcmp.f32 s15, s14
    vmrs    APSR_nzcv, fpscr
    bge     ltick_out
    vldr    s14, lhalf
    vadd.f32 s15, s15, s14
    vcvt.s32.f32 s15, s15           /* rounded BPM */
    vmov    r5, s15
    cmp     r5, #40
    it      lo
    movlo   r5, #40
    cmp     r5, #250
    it      hi
    movhi   r5, #250
    ldr     r0, =0x{SONG_STORE:08x}
    movs    r1, #0x66
ltick_get:
    .short  0, 0
    cmp     r0, r5
    beq     ltick_out
    ldr     r0, =0x{SONG_STORE:08x}
    movs    r1, #0x66
    mov     r2, r5
ltick_set:
    .short  0, 0
    ldr     r0, =0x{L_PEND:08x}     /* and the engine's fallback, next block */
    uxth    r1, r5
    movt    r1, #0x{L_PEND_MAGIC:x}
    str     r1, [r0]
    ldr     r0, =0x{SESSION_DIRTY:08x}  /* mark the preset changed, as stock edits do */
    movs    r1, #1
    strb    r1, [r0]
ltick_out:
    vpop    {{d7}}
    pop     {{r0, r1, r2, r3, r4, r5, r6, r7, r8, r12, lr}}
ltick_next:
    .short  0, 0

    .balign 4
lmin:
    .float  20.0
lmax:
    .float  1000.0
lhalf:
    .float  0.5
    .ltorg
    .balign 4
llabel:
    .asciz  "Preset BPM:"
    .balign 4
"""
    code, syms = assemble("hookL", src)
    code = bytearray(code)
    for sym, target, call in (
        ("lreg_next", 0x08069ed4, False),
        ("lgetall_next", 0x080894d4, False),
        ("lsetall_next", 0x080894fe, False),
        ("lpost_next", post_next, False),
        ("lctor_next", ctor_next, False),
        ("ltick_get", 0x080894d0, True),
        ("ltick_set", 0x080894fa, True),
        ("ltick_next", tick_next, False),
        ("lblock_next", block_next, False),
    ):
        off = syms[sym]
        code[off:off + 4] = enc_b_bl(org + off, target, call)
    return bytes(code), syms


def apply_preset_bpm(d, j_org, j_syms):
    """Guard L's sites, append L, install it, chain J's post and ctor tails."""
    for site, target, _ in L_CALLS:
        if branch_target(d, site) != target:
            refuse(f"The Preset BPM call at {site:#x} is not what it should be.")
    for entry, stock, _, _ in L_ENTRIES:
        _guard(d, entry, stock, "settings store entry")
    for va in SESSION_MGR_GUARDS:
        if struct.unpack_from("<I", d, va - BASE)[0] != SESSION_MGR:
            refuse(f"The session literal at {va:#x} is not what it should be.")
    for sym, stock in (("jpost_next", 0x080937f4), ("jctor_next", 0x080a625c)):
        if branch_target(d, j_org + j_syms[sym]) != stock:
            refuse(f"Hook J's {sym} is not what it should be.")
    code, syms = build_preset_bpm_hook(
        BASE + len(d),
        post_next=branch_target(d, j_org + j_syms["jpost_next"]),
        tick_next=branch_target(d, 0x0805bbb4),
        ctor_next=branch_target(d, j_org + j_syms["jctor_next"]),
        block_next=branch_target(d, 0x08093e72))
    org = _append(d, bytearray(code))
    for site, _, entry in L_CALLS:
        d[site - BASE:site - BASE + 4] = enc_b_bl(site, org + syms[entry], True)
    for entry, _, hook, _ in L_ENTRIES:
        d[entry - BASE:entry - BASE + 4] = enc_b_bl(entry, org + syms[hook], False)
    for sym, entry in (("jpost_next", "lpost"), ("jctor_next", "lctor")):
        at = j_org + j_syms[sym]
        d[at - BASE:at - BASE + 4] = enc_b_bl(at, org + syms[entry], False)
    vprint(f"L Preset BPM {len(code)} bytes at {org:#010x}")
    return org, syms


# --- N: the Character chain (opt-in test build) ---------------------------------
# Four chained Airwindows effects on the master bus -- Destruction, Drive,
# Colour, Saturation -- after the delay/reverb returns and before the master EQ
# and compressor. The DSP is C (character/character.c, compiled, reduced and
# frozen in FROZEN_C["chr"], linked here); this section adds two asm shims.
#
# The effects are ports of Airwindows plugins by Chris Johnson / airwindows,
# used under the MIT licence. Copyright (c) 2018 Chris Johnson. The full MIT
# notice -- permission notice and warranty disclaimer -- is reproduced at the
# top of character/character.c and travels with the frozen object's source.
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND.
#
# WHERE IT HOOKS. The engine block routine 0x08096530 (called once per audio
# block from 0x0809418a, inside the profiled per-block function 0x08093e48)
# sums the pads into master buffer 0, then mixes in buffer 0xd (delay return,
# `bl 0x080a3328` at 0x08096cf2) and buffer 0xe (reverb return, the same call
# at 0x08096d04), then calls the EQ chain head at engine+0x28cec (the master
# EQ, constructed with node id 0 = buffer 0), then the compressor if it is on,
# then renders the outputs. N retargets the SECOND mix call: the stock mix runs
# first, unchanged, and then the chain runs over buffer 0.
#
# THE REGISTER CONTRACT. nblock calls the original, then saves EVERYTHING the
# compiled C could change -- r0-r3, r12, lr, APSR, FPSCR, s0-s15 -- as they
# stand AFTER the original returned, and restores them before returning. So
# the caller sees exactly the original callee's register effects whatever the
# C does. nmidi does the same around chr_cc and then branches on to the next
# hook with every register as it found it (r0/r1 are the thunk's arguments).
CHARACTER_VERSION = "2.3.9-chr4"
CHR_RAM = 0x2406c200                 # store +0; engine +0x40; OSD +0x100; dry +0x200
assert CHR_RAM >= L_PEND + 4 + 0x100
CHR_SITE, CHR_SITE_FN = 0x08096d04, 0x080a3328     # reverb-return mix into bus 0
CHR_SITE_CONTEXT = (0x08096cf8, "18ee900a11f0dcfa01462046")  # vmov r0,s17; bl; mov r1,r0; mov r0,r4
CHR_EQ_HEAD_VA, CHR_EQ_HEAD = 0x0809702c, 0x00028cec          # literal: the chain head after us
CHR_CC_FIRST, CHR_CC_LAST = 14, 29   # channel 1: 14-17 Amount, 18-21 Tone, 22-25 Type, 26-29 Next
CHR_SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "character", "character.c")
# The work area: the dry copies (2 x 256 floats), then the four slot states
# packed to their real sizes (character.c: CHR_SLOTn_OFF/BYTES, one static
# assert per slot). DEVICE memory under the firmware's own MPU setup (regions
# 4/5 at 0x24060000/0x24070000: TEX0 C0 B1, XN), as it always was.
# ch12: up to ch11 the slots were 0x3000 apart, to 0x24078c00, inside the
# stock uncached pool's preset/stream buffer -- see POOL_ below. The untested
# --fast-ram variant (work area in D2 SRAM2) was removed with it; D2 SRAM
# (0x30000000-0x30047fff: no reference of any kind in the stock image, clocked
# by SystemInit, cacheable under MPU region 9) remains a future option.
CHR_WORK_DEFAULT = CHR_RAM + 0x200
CHR_SLOTS = (                        # (offset into the work area, reserved bytes)
    (0x0800, 0x0100),                # slot 0 Destruction, 0x2406cc00 (largest state 72 B)
    (0x0900, 0x0100),                # slot 1 Overdrive,   0x2406cd00 (104 B)
    (0x0b00, 0x2000),                # slot 2 Colour,      0x2406cf00 (ToTape8, 8172 B)
    (0x0a00, 0x0100),                # slot 3 Saturation,  0x2406ce00 (28 B)
)
CHR_WORK_BYTES = max(o + n for o, n in CHR_SLOTS)    # 0x2b00: to 0x2406ef00


def build_character_hook(org, midi_next, features=0):
    obj = c_object("chr", CHR_SRC)
    ccode, csyms = c_link(obj, org)
    wb = csyms["chr_work_base"]
    if struct.unpack_from("<I", ccode, wb)[0] != CHR_WORK_DEFAULT:
        refuse("The Character work-area word is not what the patcher expects.")
    fb = csyms["chr_features"]
    if struct.unpack_from("<I", ccode, fb)[0] != 0:
        refuse("The Character features word is not what the patcher expects.")
    if features:
        ccode = bytearray(ccode)
        struct.pack_into("<I", ccode, fb, features)
        ccode = bytes(ccode)
    sorg = org + len(ccode)
    src = """
.syntax unified
.thumb
.section .text
.global _sN
.thumb_func
_sN:
nblock:                             /* replaces bl 0x080a3328 at 0x08096d04 */
    push    {r0, lr}                /* r0 = master bus buffer 0 */
nblock_orig:
    .short  0, 0                    /* bl 0x080a3328: the stock mix, unchanged */
    push    {r0, r1, r2, r3, r12, lr}   /* the state the original left */
    mrs     r0, apsr
    vmrs    r1, fpscr
    push    {r0, r1}
    vpush   {s0-s15}
    ldr     r0, [sp, #96]           /* 64 + 8 + 24: our saved r0 */
nblock_c:
    .short  0, 0                    /* bl chr_block */
    vpop    {s0-s15}
    pop     {r0, r1}
    msr     apsr_nzcvq, r0
    vmsr    fpscr, r1
    pop     {r0, r1, r2, r3, r12, lr}
    add     sp, #4
    pop     {pc}

nmidi:                              /* chained at the MIDI CC post, before F */
    push    {r0, r1, r2, r3, r12, lr}
    mrs     r0, apsr
    vmrs    r1, fpscr
    push    {r0, r1}
    vpush   {s0-s15}
    ldr     r0, [sp, #224]          /* 96 + 0x80: MIDI channel, 0-15 */
    ldr     r1, [sp, #228]          /* 96 + 0x84: CC number */
    ldr     r2, [sp, #232]          /* 96 + 0x88: value, 7-bit << 7 */
nmidi_c:
    .short  0, 0                    /* bl chr_cc */
    vpop    {s0-s15}
    pop     {r0, r1}
    msr     apsr_nzcvq, r0
    vmsr    fpscr, r1
    pop     {r0, r1, r2, r3, r12, lr}
nmidi_next:
    .short  0, 0                    /* b.w the next hook, as found */
    .balign 4
"""
    code, syms = assemble("hookN", src)
    code = bytearray(code)
    for sym, target, call in (
        ("nblock_orig", CHR_SITE_FN, True),
        ("nblock_c", org + csyms["chr_block"], True),
        ("nmidi_c", org + csyms["chr_cc"], True),
        ("nmidi_next", midi_next, False),
    ):
        off = syms[sym]
        code[off:off + 4] = enc_b_bl(sorg + off, target, call)
    allsyms = {f"c_{k}": v for k, v in csyms.items()}
    allsyms.update({k: len(ccode) + v for k, v in syms.items()})
    return ccode + bytes(code), allsyms


def apply_character(d, features=0):
    """Guard N's sites, append the C object and the shims, install both."""
    if bytes(d[CHR_SITE - BASE:CHR_SITE - BASE + 4]) != enc_b_bl(CHR_SITE, CHR_SITE_FN, True):
        refuse(f"The master bus mix call at {CHR_SITE:#x} is not what it should be.")
    _guard(d, *CHR_SITE_CONTEXT, "master bus mix set-up")
    if struct.unpack_from("<I", d, CHR_EQ_HEAD_VA - BASE)[0] != CHR_EQ_HEAD:
        refuse(f"The EQ chain literal at {CHR_EQ_HEAD_VA:#x} is not what it should be.")
    midi_next = branch_target(d, MIDI_POST_CALL)
    if midi_next == MIDI_POST_FN or midi_next < BASE + STOCK_SIZE:
        refuse("The MIDI CC post call does not lead to the patch's CC hooks.")
    org = BASE + len(d)
    while org % 16:                       # the C object's constant pools align to 16
        d += b"\0"
        org += 1
    code, syms = build_character_hook(org, midi_next, features)
    org2 = _append(d, bytearray(code))
    assert org2 == org
    d[CHR_SITE - BASE:CHR_SITE - BASE + 4] = enc_b_bl(CHR_SITE, org + syms["nblock"], True)
    d[MIDI_POST_CALL - BASE:MIDI_POST_CALL - BASE + 4] = enc_b_bl(
        MIDI_POST_CALL, org + syms["nmidi"], True)
    vprint(f"N Character {len(code)} bytes at {org:#010x}, ram {CHR_RAM:#x}")
    return org, syms


# --- O: OSD pop-up (opt-in test build) ------------------------------------------
# A small centred box naming what a Character, EQ, compressor, pad-switch or
# delay-switch CC just changed, or that an external clock let go or took
# over; drawn on the UI tick into the TOP LCD layer with the firmware's own
# 6x8 font drawn at 2x (the stock UI's size), gone 1.5 s after the last
# change; plus an "OSD: OFF/ON" Settings row (id 0x14b, default ON), saved
# like Clk Loss. chr6-8 had OFF/SMALL/LARGE and saved LARGE as 2, so every
# non-zero value is ON. The drawing and the MIDI-side bookkeeping are C
# in the Character object (character/chr_osd.h); this section adds the asm:
# the registration, the global-store entry, the Settings wrapper, the UI-tick
# shim, a recorder on the clock constructor (chained after J's, as L does),
# and two head insertions in front of hooks A and B that note a pad voice
# accepting one of the OSD's pad CCs. Every hook is a HEAD insertion: it takes
# the site and branches on to whatever was installed there (A, B, J, K, L, M
# or stock). See CHARACTER-DESIGN.md section 11 for the trace.
OSD_VERSION = "2.3.9-ch12"   # "2.3.9-chr12" would be 11: past VER_DISPLAY_MAX
RELEASE_VERSION = "2.3.9-mod"  # the default output (and --v239)
PREVIOUS_VERSION = "2.3.8-mod"  # --v238: the published 2.3.8-mod, byte for byte
PREVIOUS_SHA = "1ce1162229af75f0de5e95985e4805fbc795180d34cfc13cd08dd366ce3d93e6"
# Single-feature TEST builds, layered on the 2.3.8-mod base. Never a release;
# not in the usage text. Any of them switches the default (2.3.9-mod) off.
TEST_FLAGS = ("--character", "--osd", "--cpu-meter", "--preset-bpm", "--clock-debug")
OSD_ID = 0x14b                       # unregistered in stock; no immediate anywhere
OSD_RAM_OFF = CHR_RAM + 0x100        # chr_osd_t.off: OFFMAGIC = Off
OSD_OFFMAGIC = 0x4f53444f            # OFF; any other word = ON (the default)
OSD_FONT_VA, OSD_FONT_A = 0x080bf664, "20508888f8888800"   # the 6x8 font's "A"
OSD_FONT_LIT_VA = 0x080e3e64         # .data initialiser of the font descriptor
OSD_CALLS = (                        # (site, stock target, entry, what)
    (0x0806abbc, 0x08069ed4, "oreg", "the uiflip registration"),
    (0x08089efe, 0x0808951e, "ostore", "the global store's 0xdc entry"),
    (0x0807c5be, 0x080937f4, "opost", "the Settings apply-all post"),
    (0x0807d51c, 0x080937f4, "opost", "the Settings live-edit post"),
    (0x0805bbb4, 0x08042900, "odraw", "the UI present"),
)
OSD_CLK = OSD_RAM_OFF + 0x40         # chr_osd_t.clk: the clock object, from its ctor
OSD_ACC = OSD_RAM_OFF + 0x44         # chr_osd_t.acc_seq, acc_cc, acc_val
OSD_TEXT = OSD_RAM_OFF + 0x58        # chr_osd_t.text[2][28]: the lines on screen
assert OSD_RAM_OFF + 0xb4 <= CHR_RAM + 0x200
# The OSD's UI-side state (per-parameter last-shown text hashes, tempo
# tracking, the box's one-bit mask: 3504 bytes). chr9-ch11 put it at
# 0x24078c00, inside the stock pool's preset/stream buffer; ch12 puts it in the
# free gap just above the stock main stack (character.c: CHR_OSD2_ADDR).
OSD2_RAM = 0x24069400
OSD2_BYTES = 0xe00
# The pad voices' CC chains, where hooks A and B sit: a `cmp r3, #cc` then a
# `bne.w` to the hook. O retargets each bne.w to a recorder that notes the
# voice took a CC the OSD shows -- if that voice class supports it -- and
# branches on to the hook. (site, class, value slot on the voice's frame,
# CC mask from 86: bit n = CC 86+n)
# ch11: every per-pad CC the patch adds, 79-93, as CC-REFERENCE's pad-type
# table has them. Mask bit n = CC 79+n.
OSD_PAD_SITES = (
    (0x080b2316, 1, 0x40, 0x77ff),   # class A: 79-83 (Granular only), 84-89, 91-93
    (0x080b7cd6, 2, 0x38, 0x7960),   # class B: 84, 85, 87, 90 (Slicer only), 91-93
)
OSD_SLICER_OFF = 0x254               # class B voice: 1 = Slicer (its setter: mode == 2)
OSD_SLICER_GUARD = (0x080b742a, "85f85432")  # strb.w r3, [r5, #0x254]: (mode == 2)
OSD_GRANULAR_OFF = 0x575             # class A voice: 1 = Granular (its setter: mode == 3)
OSD_GRANULAR_GUARD = (0x080b27c2, "a3f10303b3fa83f35b0984f87535")  # sub #3; clz; lsrs; strb [r4,#0x575]
# The firmware's own LFO Wave labels, indexed by the raw value hooks A/B give
# the voice (0-9). chr_osd.h reads them from flash; the patcher checks them.
# rc1 (--v239): the CPU meter's bar joins the OSD row, which becomes
# OSD: OFF / POP-UP / CPU / BOTH under a NEW xml name, `osdmode`. A new name
# because no value of the old `osd` can be told apart: chr6-8 saved LARGE as 2,
# and 2 is now CPU. The loader reads only attributes the store names, so a
# stale `osd` -- like a stale `cpumeter` -- is never read, and the row starts
# at its default, POP-UP. A value above BOTH (a hand-edited file) behaves as
# POP-UP and is written back into the store as POP-UP at the boot apply-all,
# so the row never indexes past its four labels.
OSD_MODES = ("OFF", "POP-UP", "CPU", "BOTH")   # stored 0..3; default 1
OSD_MODE_XML = "osdmode"
OSD_SETPARAM = 0x080894fa            # SetParam(store, id, value); L may sit on its entry
OSD_LFO_LABELS = 0x080c478c
OSD_LFO_NAMES = ("Saw", "Rev Saw", "Triangle", "Pos Tri", "Sine", "Pos Sine",
                 "Square", "Pos Square", "Random", "None")


def bne_target(d, at):
    """Destination of the Thumb-2 bne.w at `at` (T3, cond NE)."""
    hw1, hw2 = struct.unpack_from("<HH", d, at - BASE)
    assert hw1 & 0xfbc0 == 0xf040 and hw2 & 0xd000 == 0x8000, f"no bne.w at {at:#x}"
    s = (hw1 >> 10) & 1
    imm = (s << 20) | (((hw2 >> 11) & 1) << 19) | (((hw2 >> 13) & 1) << 18) \
        | ((hw1 & 0x3f) << 12) | ((hw2 & 0x7ff) << 1)
    if s:
        imm -= 1 << 21
    return at + 4 + imm


def _osd_pad_rec(tag, cls, vslot, mask, slicer, keep=False):
    """A voice is about to take a CC (r3 = number, r4 = voice, value on the
    frame). If its class supports that CC, bump the acceptance record. Saves
    and restores every register it touches, and the flags. Writes exactly
    three words, at OSD_ACC; no index of any kind."""
    if slicer:
        extra = f"""
    cmp     r3, #90
    bne     o{tag}_take
    ldrb.w  r1, [r4, #{OSD_SLICER_OFF:#x}]
    cbz     r1, o{tag}_out              /* Play Through on a Clip pad: ignored */"""
    else:
        extra = f"""
    cmp     r3, #83
    bhi     o{tag}_take
    ldrb.w  r1, [r4, #{OSD_GRANULAR_OFF:#x}]
    cbz     r1, o{tag}_out              /* granular CCs on a Sample pad: ignored */"""
    return f"""
opad{tag}:                              /* r3 = CC, r4 = voice; flags NE */
    push    {{r0, r1, r2, r12}}
    mrs     r12, apsr
    sub.w   r0, r3, #79
    cmp     r0, #14
    bhi     o{tag}_out
    movs    r1, #1
    lsls    r1, r1, r0
    movw    r2, #{mask:#x}
    tst     r1, r2
    beq     o{tag}_out{extra}
o{tag}_take:{_keep_skip(tag) if keep else ""}
    ldr     r2, [sp, #{vslot + 16:#x}]  /* the value, past our 16-byte push */
    ldr     r0, =0x{OSD_ACC:08x}
    movw    r1, #{cls << 8:#x}
    orrs    r1, r3
    str     r1, [r0, #4]
    str     r2, [r0, #8]
    ldr     r1, [r0]
    adds    r1, #1
    str     r1, [r0]                    /* last: the count says it is complete */
o{tag}_out:
    msr     apsr_nzcvq, r12
    pop     {{r0, r1, r2, r12}}
opad{tag}_next:
    .short  0, 0
"""


def _osd_post(modes):
    if not modes:
        return f"""
opost:                              /* PostSetParam(r0 mgr, r1 key, r2 id, r3 value) */
    movw    r12, #0x{OSD_ID:x}
    cmp     r2, r12
    bne     opost_next
    ldr     r12, =0x{OSD_RAM_OFF:08x}
    cbz     r3, opost_off
    movs    r3, #0                  /* ANY non-zero is ON: chr6-8 saved 2 (LARGE) */
    str     r3, [r12]
    bx      lr
opost_off:
    ldr     r3, =0x{OSD_OFFMAGIC:08x}
    str     r3, [r12]
    bx      lr
opost_next:
    .short  0, 0
"""
    return f"""
opost:                              /* PostSetParam(r0 mgr, r1 key, r2 id, r3 value) */
    movw    r12, #0x{OSD_ID:x}
    cmp     r2, r12
    bne     opost_next
    push    {{r4, lr}}              /* ours: NOT posted to the engine */
    cmp     r3, #3
    bls     opost_mode
    ldr     r0, =0x{GLOBAL_STORE:08x}   /* out of range: POP-UP, and in the store too */
    mov     r1, r2
    movs    r2, #1
opost_set:
    .short  0, 0                    /* bl SetParam */
    movs    r3, #1
opost_mode:                         /* 0 OFF, 1 POP-UP, 2 CPU, 3 BOTH */
    movs    r1, #0
    ldr     r0, =0x{OSD_OFFMAGIC:08x}
    tst     r3, #1                  /* pop-ups in POP-UP and BOTH */
    it      ne
    movne   r0, r1
    ldr     r12, =0x{OSD_RAM_OFF:08x}
    str     r0, [r12]
    ldr     r0, =0x{CLK_MAGIC:08x}
    cmp     r3, #2                  /* the bar in CPU and BOTH */
    it      lo
    movlo   r0, r1
    ldr     r12, =0x{K_MODE:08x}
    str     r0, [r12]
    pop     {{r4, pc}}
opost_next:
    .short  0, 0
"""


def _keep_skip(tag):
    """rc1: while a Keep CCs replay is in flight (P sets KEEP_REPLAY on the
    audio thread for the block it posts in), a voice taking a replayed CC is
    not an acceptance: no pop-up can come of it."""
    return f"""
    ldr     r0, =0x{KEEP_RAM + KEEP_REPLAY_OFF:08x}
    ldr     r0, [r0]
    ldr     r1, =0x{KEEP_REPLAY_MAGIC:08x}
    cmp     r0, r1
    beq     o{tag}_out"""


def build_osd_hook(org, nexts, tick_fn, modes=False):
    """`modes` is the release row (rc1): four values under `osdmode`, the
    CPU meter's bar switched from here, and pad recorders that ignore a Keep
    CCs replay. Without it, the ch11 OFF/ON row."""
    pads = "".join(_osd_pad_rec(t, c, v, m, c == 2, keep=modes)
                   for t, (_, c, v, m) in zip("AB", OSD_PAD_SITES))
    if modes:
        tail = f"""
ovalues:                            /* OFF is the firmware's own string */
    .word   0x080c0030, 0, 0, 0     /* the rest: filled in per origin */
    .balign 4
ostr_popup:
    .asciz  "POP-UP"
    .balign 4
ostr_cpu:
    .asciz  "CPU"
    .balign 4
ostr_both:
    .asciz  "BOTH"
    .balign 4
olabel:
    .asciz  "OSD:"
    .balign 4
oxml:
    .asciz  "{OSD_MODE_XML}"
    .balign 4
"""
    else:
        tail = """
ovalues:                            /* two values; the third word is a spare */
    .word   0x080c0030, 0x080c0034, 0x080c0034   /* "ON", so a stale 2 shows ON */
    .balign 4
olabel:
    .asciz  "OSD:"
    .balign 4
oxml:
    .asciz  "osd"
    .balign 4
"""
    src = f"""
.syntax unified
.thumb
.section .text
.global _sO
.thumb_func
_sO:
oreg:                               /* r0 = registry; [sp], [sp+4] = the uiflip call's args */
    push    {{r0, r1, r2, r3, r4, r12, lr}}
    sub     sp, #12
    movw    r1, #0x{OSD_ID:x}
    adr.w   r2, olabel
    adr.w   r3, ovalues
    movs    r4, #{len(OSD_MODES) if modes else 2}
    str     r4, [sp]
    adr.w   r4, oxml
    str     r4, [sp, #4]
oreg_call:
    .short  0, 0                    /* enum registrar */
    add     sp, #12
    pop     {{r0, r1, r2, r3, r4, r12, lr}}
oreg_next:
    .short  0, 0

ostore:                             /* r0 = the global store */
    push    {{r0, r1, r2, r3, r12, lr}}
    movw    r1, #0x{OSD_ID:x}
    movs    r2, #1                  /* default ON (rc1: POP-UP) */
ostore_call:
    .short  0, 0                    /* AddParam */
    pop     {{r0, r1, r2, r3, r12, lr}}
ostore_next:
    .short  0, 0
{_osd_post(modes)}
odraw:                              /* r0 = the display object, every UI tick */
    push    {{r0, r1, r2, r3, r12, lr}}
    mrs     r1, apsr
    vmrs    r2, fpscr
    push    {{r1, r2}}
    vpush   {{s0-s15}}
odraw_c:
    .short  0, 0                    /* bl chr_osd_tick(display) */
    vpop    {{s0-s15}}
    pop     {{r1, r2}}
    msr     apsr_nzcvq, r1
    vmsr    fpscr, r2
    pop     {{r0, r1, r2, r3, r12, lr}}
odraw_next:
    .short  0, 0

octor:                              /* chained after J's jctor; r4 = the clock */
    ldr     r12, =0x{OSD_CLK:08x}
    str     r4, [r12]
    mov.w   r12, #0                 /* as J leaves it: registers exactly as without O */
octor_next:
    .short  0, 0
{pads}
    .balign 4
    .ltorg
{tail}"""
    code, syms = assemble("hookOm" if modes else "hookO", src)
    code = bytearray(code)
    if modes:
        for k, sym in enumerate(("ostr_popup", "ostr_cpu", "ostr_both"), 1):
            struct.pack_into("<I", code, syms["ovalues"] + 4 * k, org + syms[sym])
        off = syms["opost_set"]
        code[off:off + 4] = enc_b_bl(org + off, OSD_SETPARAM, True)
    for sym, target, call in (
        ("oreg_call", 0x08069e7c, True),
        ("oreg_next", nexts["oreg"], False),
        ("ostore_call", 0x0808951e, True),
        ("ostore_next", nexts["ostore"], False),
        ("opost_next", nexts["opost"], False),
        ("odraw_c", tick_fn, True),
        ("odraw_next", nexts["odraw"], False),
        ("octor_next", nexts["octor"], False),
        ("opadA_next", nexts["opadA"], False),
        ("opadB_next", nexts["opadB"], False),
    ):
        off = syms[sym]
        code[off:off + 4] = enc_b_bl(org + off, target, call)
    return bytes(code), syms


def _cstr(d, va):
    o = va - BASE
    if not 0 <= o < len(d):
        return None
    e = d.find(b"\0", o)
    return bytes(d[o:e]).decode("latin1") if e > o else None


def apply_osd(d, n_org, n_syms, j_org, j_syms, modes=False):
    """Guard O's sites, the font and the labels, append O, take each site.
    `modes`: the release row, which also switches K's bar (K must be in)."""
    if bytes(d[OSD_FONT_VA + 33 * 8 - BASE:OSD_FONT_VA + 34 * 8 - BASE]).hex() != OSD_FONT_A:
        refuse("The firmware's 6x8 font is not where the OSD expects it.")
    for i, want in enumerate(OSD_LFO_NAMES):
        if _cstr(d, struct.unpack_from("<I", d, OSD_LFO_LABELS + 4 * i - BASE)[0]) != want:
            refuse("The LFO Wave labels are not where the OSD expects them.")
    _guard(d, OSD_SLICER_GUARD[0], OSD_SLICER_GUARD[1], "class B mode setter")
    _guard(d, OSD_GRANULAR_GUARD[0], OSD_GRANULAR_GUARD[1], "class A mode setter")
    if struct.unpack_from("<I", d, OSD_FONT_LIT_VA - BASE)[0] != OSD_FONT_VA:
        refuse("The font descriptor's initialiser is not what the OSD expects.")
    _guard(d, CLK_REG_LIMIT_VA, CLK_REG_LIMIT, "parameter registry limit")
    _guard(d, CLK_STORE_CAP_VA, CLK_STORE_CAP, "global settings capacity")
    nexts = {}
    for site, stock, entry, what in OSD_CALLS:
        t = branch_target(d, site)
        if t != stock and t < BASE + STOCK_SIZE:
            refuse(f"The call at {site:#x} ({what}) is not what the OSD expects.")
        if entry in nexts and nexts[entry] != t:
            refuse("The two Settings post sites no longer lead to the same place.")
        nexts[entry] = t
    ctor_next = branch_target(d, j_org + j_syms["jctor_next"])
    if ctor_next != 0x080a625c and ctor_next < BASE + STOCK_SIZE:   # stock, or L's lctor
        refuse("Hook J's jctor_next is not what the OSD expects.")
    nexts["octor"] = ctor_next
    for (site, _, _, _), tag in zip(OSD_PAD_SITES, "AB"):
        t = bne_target(d, site)
        if t < BASE + STOCK_SIZE:
            refuse(f"The pad CC chain at {site:#x} does not lead to hook {tag}.")
        nexts[f"opad{tag}"] = t
    org = BASE + len(d)
    code, syms = build_osd_hook(org, nexts, n_org + n_syms["c_chr_osd_tick"], modes)
    org2 = _append(d, bytearray(code))
    assert org2 == org
    for site, _, entry, _ in OSD_CALLS:
        d[site - BASE:site - BASE + 4] = enc_b_bl(site, org + syms[entry], True)
    at = j_org + j_syms["jctor_next"]
    d[at - BASE:at - BASE + 4] = enc_b_bl(at, org + syms["octor"], False)
    for (site, _, _, _), tag in zip(OSD_PAD_SITES, "AB"):
        d[site - BASE:site - BASE + 4] = enc_bne_w(site, org + syms[f"opad{tag}"])
    vprint(f"O OSD {len(code)} bytes at {org:#010x}, id {OSD_ID:#x}, ram {OSD_RAM_OFF:#x}")
    return org, syms


# --- P: Keep CCs (rc1, --v239) ----------------------------------------------------
# A "Keep CCs: OFF/ON" Settings row (id 0x166, default OFF, saved like Clk
# Loss). ON: after a preset load completes, the last value of every patch pad
# CC (79-93) received since power-on is sent again, per MIDI channel, so the
# new preset's pads take the controller's positions. Untouched (channel, CC)
# pairs are never sent, so they keep the preset's own values. Nothing else is
# ever sent: no stop CCs (120/123), no Character, EQ, compressor or delay CC.
#
# THE PATH AND THE THREAD. A real CC is parsed AND queued on the audio task:
# 0x08042464/0x08042480 call the per-block function 0x08093e48 (this =
# MANAGER) once per DMA half; it builds a 24-byte type-7 event and posts it at
# 0x08093f14 through the thunk 0x080950c0 into the key-0x400 queue at
# *(MANAGER+0xa204) + 0x127dc (64 entries, count at +0x600). The append
# (0x080a396c) has no lock, so the audio task is the only producer that cannot
# race the dispatcher; the engine routine 0x08096530, later in the same call,
# drains the UI's command ring (+0xdcd8 read / +0xdcdc write) and then
# dispatches the queues to the pads. P posts the SAME event through the SAME
# thunk from the block start (0x08093e72), before the MIDI loop and before any
# DSP: the queue then carries it to every pad on that channel, exactly as a
# live CC (two pads on a channel both take it; a pad type without that CC
# ignores it, as it would a live one).
#
# WHEN. FinalizeBankLoading 0x0807aa9c (UI thread; its two callers are the
# success paths of a load) ends by setting the engine's ready byte
# MANAGER+0xae10 to 1 (0x0807af2c); LoadBank cleared it. P wraps both calls and,
# with the row ON, bumps a load sequence. The block start then waits until the
# engine is ready AND the UI's command ring is empty -- every parameter the
# load posted has been applied -- and posts at most 16 events per block while
# the queue holds no more than 16 (real CCs share it). A new load restarts the
# replay; turning the row OFF stops it at the next block.
#
# NOTES ARE NOT CCs (rc3). Stock sends note-on/off and poly aftertouch through
# the same post at 0x08093f14 (they share the CC tail 0x08093f0c), with the note
# number in the CC slot and the raw velocity in the value slot. P's head there
# checks the type byte ([sp,#0x78], 7 = CC) and sends anything else straight to
# the stock post, so every CC hook behind it (P, N/O, F, D, C) sees CCs only.
# rc2 recorded the sequencer's note 87 as "CC 87" and replayed it as Sustain.
#
# NO POP-UPS. Replayed events never pass 0x08093f14, so the OSD sees no
# request; and while P has events in flight it sets KEEP_REPLAY, which O's pad
# recorders (hookOm) treat as "not an acceptance". A live pad CC arriving in
# that same 2.7 ms block loses only its pop-up.
#
# PRESET BPM IS A PRESET EDIT (rc2). Stock marks the preset unsaved
# (SessionMgr+0x8004) on its own edit paths; the Settings live edit (handler B)
# does not, because Settings rows are global. A Preset BPM edit there was only
# marked by the engine's tempo echo (message 0x6f, 0x08076c8c), which never
# comes when L parks the edit under a running clock: rc1's manual edits were
# not saveable with a clock. P's head at the LIVE-EDIT post site (0x0807d51c,
# only ever reached from handler B: a user's edit) sets the byte for id 0x66.
# Preset loads post the song store elsewhere (0x0807ad0e-0x0807adec, straight
# to PostSetParam) and the boot apply-all never carries 0x66 (not a global
# store entry), so neither marks anything.
KEEP_ID = 0x166                      # unregistered in stock, no immediate anywhere
KEEP_RAM = 0x2406a400                # in the guarded gap; cleared by the clock ctor
KEEP_BYTES = 0x220
KEEP_MODE_OFF, KEEP_LOAD_OFF, KEEP_DONE_OFF, KEEP_IDX_OFF, KEEP_REPLAY_OFF = 0, 4, 8, 0xc, 0x10
KEEP_BITS_OFF, KEEP_VALS_OFF = 0x20, 0x40   # u16 per channel; u16 per (channel, CC)
KEEP_ON = 0x4b454550                 # "KEEP": the row is ON; any other word is OFF
KEEP_REPLAY_MAGIC = 0x5245504c       # "REPL"
KEEP_CC_FIRST, KEEP_CC_COUNT = 79, 15
KEEP_SLOTS = 16 * KEEP_CC_COUNT      # 240
KEEP_PER_BLOCK = 16
assert KEEP_VALS_OFF + 2 * KEEP_SLOTS <= KEEP_BYTES
FINALIZE_FN = 0x0807aa9c
KEEP_FINAL_SITES = (0x0807c4e0, 0x0807f98c)
ENG_X, ENG_READY = 0xa204, 0xae10
RING_R, RING_W = 0xdcd8, 0xdcdc
QUEUE_COUNT = 0x127d8 + 4 + 0x600    # the key-0x400 queue's count
KEEP_GUARDS = (                      # (va, bytes, what): the facts P rests on
    (0x08093e82, "4af6106314f80390", "the per-block function reads the ready byte"),
    (0x0807af2c, "4af610630122f254", "FinalizeBankLoading sets the ready byte"),
    (0x08093e94, "4af20427", "the dispatcher's engine pointer is this+0xa204"),
    (0x08093f10, "38682293", "the dispatcher posts *(this+0xa204)"),
    (0x08093ee6, "07232846cdf87c908df87830", "the dispatcher's event: type 7, +4 = 0"),
    (0x080950c0, "034603484ff48062184413f06bb9", "the MIDI thunk, key 0x400"),
    (0x080a83e2, "04221044fbf7c1ba", "key 0x400 is queue +4"),
    (0x080a396c, "f0b4d0f800763f2f0ed807eb", "the queue append: count +0x600, 64 entries"),
    (0x0809654e, "4df6dc434df6d842", "the engine routine's command ring indices"),
    (0x08042488, "78290224", "the audio task's engine literal is MANAGER"),
    (0x08097bdc, "49f23c73c35853b948f2d43248f2d833c3588058181ab0fa80f040097047",
     "the loader-idle test: +0x973c == 0 and +0x83d4 == +0x83d8"),
    (0x0807c26e, "b24817f05efd", "LoadBank asks it, with the literal at 0x0807c538"),
    (0x08093d30, "03f054bf", "through this tail call"),
    (0x08097b8c, "48f6e012034670b548f6e414045980588442", "the reply ring: +0x89e0 read, +0x89e4 write"),
    (0x0807f892, "08a9284614f099f8", "the UI dispatcher reads it with r5 ..."),
    (0x0807f874, "7f4d", "... = the literal at 0x0807fa74"),
    (0x080939cc, "04f0deb8", "through this tail call"),
    (0x08093eea, "cdf87c908df87830", "type 7 for a CC at +0x78 ..."),
    (0x08093fea, "8df87830cdf87c90", "... type 5 for a note-on, into the same tail"),
)
KEEP_SETPARAM = 0x080894fa
# rc3: the replay waits for the firmware's own "loader idle" test (0x08097bdc,
# which LoadBank asks at 0x0807c270 with MANAGER), the streamer-to-UI reply
# ring (read by 0x08097b8c with MANAGER, from the UI dispatcher at 0x0807f896)
# being empty, plus rc2's ready byte and empty command ring -- all held for
# KEEP_STEADY_BLOCKS consecutive blocks. If the loader never goes idle, it
# replays anyway after KEEP_TIMEOUT_BLOCKS (but never without the ready byte
# and an empty command ring).
LOADER_BUSY, LOADER_R, LOADER_W = 0x973c, 0x83d4, 0x83d8
REPLY_R, REPLY_W = 0x89e0, 0x89e4
KEEP_STABLE_OFF, KEEP_AGE_OFF = 0x14, 0x18
KEEP_STEADY_BLOCKS = 32              # ~85 ms
KEEP_TIMEOUT_BLOCKS = 750            # ~2 s after the load finished
MSG_TYPE_SP = 0x78                   # the event's type byte on the dispatcher's frame (7 = CC)

# --- Auto makeup (CC 46, rc1): its own flag record -------------------------------
# COMPRESSOR-DESIGN.md section 6 put the flag at the store's +0x18, which since
# b5 is the checksum hook E verifies; by the user's decision (Option 1) it is a
# record of its own and the 2.3.6-mod store format is untouched. ON is exactly
# {AUTO_MAGIC, ~AUTO_MAGIC}; anything else is OFF. Cleared at boot with Keep
# CCs' RAM (P's link after J's clock constructor): auto makeup starts OFF at
# every power-on. Hook Dr reads and writes it; the OSD reads it.
AUTO_RAM = KEEP_RAM + KEEP_BYTES     # 0x2406a620, 8 bytes
AUTO_MAGIC = 0x4f545541              # "AUTO"
KEEP_CLEAR = KEEP_BYTES + 8          # P's boot clear covers both
CHR_FEATURE_AUTO = 1                 # chr_features bit: the OSD may show CC 46


def build_keep_hook(org, nexts):
    src = f"""
.syntax unified
.thumb
.section .text
.global _sP
.thumb_func
_sP:
preg:                               /* r0 = registry; as O's oreg */
    push    {{r0, r1, r2, r3, r4, r12, lr}}
    sub     sp, #12
    movw    r1, #0x{KEEP_ID:x}
    adr.w   r2, plabel
    adr.w   r3, pvalues
    movs    r4, #2
    str     r4, [sp]
    adr.w   r4, pxml
    str     r4, [sp, #4]
preg_call:
    .short  0, 0                    /* enum registrar */
    add     sp, #12
    pop     {{r0, r1, r2, r3, r4, r12, lr}}
preg_next:
    .short  0, 0

pstore:                             /* r0 = the global store */
    push    {{r0, r1, r2, r3, r12, lr}}
    movw    r1, #0x{KEEP_ID:x}
    movs    r2, #0                  /* default OFF */
pstore_call:
    .short  0, 0                    /* AddParam */
    pop     {{r0, r1, r2, r3, r12, lr}}
pstore_next:
    .short  0, 0

pedit:                              /* the LIVE-EDIT post only (0x0807d51c): a user's edit */
    cmp     r2, #0x66
    bne     ppost
    ldr     r12, =0x{SESSION_DIRTY:08x}  /* Preset BPM: mark the preset unsaved, as */
    push    {{r0}}                   /* stock edits do; L may park it (no echo) */
    movs    r0, #1
    strb    r0, [r12]
    pop     {{r0}}
ppost:                              /* PostSetParam(r0 mgr, r1 key, r2 id, r3 value) */
    movw    r12, #0x{KEEP_ID:x}
    cmp     r2, r12
    bne     ppost_next
    push    {{r4, lr}}              /* ours: NOT posted to the engine */
    cmp     r3, #1
    bls     ppost_mode
    ldr     r0, =0x{GLOBAL_STORE:08x}   /* out of range: OFF, and in the store too */
    mov     r1, r2
    movs    r2, #0
ppost_set:
    .short  0, 0                    /* bl SetParam */
    movs    r3, #0
ppost_mode:
    ldr     r12, =0x{KEEP_RAM:08x}
    movs    r0, #0
    cbz     r3, ppost_store
    ldr     r0, =0x{KEEP_ON:08x}
ppost_store:
    str     r0, [r12, #{KEEP_MODE_OFF}]
    pop     {{r4, pc}}
ppost_next:
    .short  0, 0

pfinal:                             /* replaces bl FinalizeBankLoading (UI thread) */
    push    {{r4, lr}}
pfinal_call:
    .short  0, 0                    /* bl FinalizeBankLoading, r0 as given */
    push    {{r0, r1, r2, r3}}          /* as the original left them, flags too */
    mrs     r3, apsr
    ldr     r4, =0x{KEEP_RAM:08x}
    ldr     r0, [r4, #{KEEP_MODE_OFF}]
    ldr     r1, =0x{KEEP_ON:08x}
    cmp     r0, r1
    bne     pfinal_out
    ldr     r0, [r4, #{KEEP_LOAD_OFF}]
    adds    r0, #1
    str     r0, [r4, #{KEEP_LOAD_OFF}]  /* the only writer of LOAD */
pfinal_out:
    msr     apsr_nzcvq, r3
    pop     {{r0, r1, r2, r3}}
    pop     {{r4, pc}}

pctor:                              /* chained after J's jctor: boot, before audio */
    push    {{r0, r1, r2, r3}}
    mrs     r3, apsr
    ldr     r0, =0x{KEEP_RAM:08x}
    movs    r1, #0
    movw    r2, #{KEEP_CLEAR:#x}       /* Keep CCs and the auto-makeup flag */
pctor_clr:
    subs    r2, #4
    str     r1, [r0, r2]
    bne     pctor_clr
    movs    r1, #{KEEP_SLOTS}
    str     r1, [r0, #{KEEP_IDX_OFF}]   /* nothing to replay */
    msr     apsr_nzcvq, r3
    pop     {{r0, r1, r2, r3}}
    mov.w   r12, #0                 /* as J leaves it */
pctor_next:
    .short  0, 0

pmidi:                              /* at 0x08093f14, before every other CC hook */
    ldrb    r3, [sp, #{MSG_TYPE_SP:#x}]  /* THE TYPE GATE (rc3): 7 = a CC. r3 is dead: */
    cmp     r3, #7                      /* the stock callee's first act is mov r3, r0 */
    beq     pmidi_cc
pmidi_stock:
    .short  0, 0                        /* b.w 0x080950c0: a note or aftertouch, as stock */
pmidi_cc:
    push    {{r0, r1, r2, r3, r4, r12}}
    mrs     r12, apsr
    ldr     r0, [sp, #{MSG_CHAN_SP + 24:#x}]
    cmp     r0, #15
    bhi     pmidi_out
    ldr     r1, [sp, #{MSG_CC_SP + 24:#x}]
    subs    r1, #{KEEP_CC_FIRST}
    cmp     r1, #{KEEP_CC_COUNT - 1}
    bhi     pmidi_out
    ldr     r2, [sp, #{MSG_VAL_SP + 24:#x}]
    ldr     r3, =0x{KEEP_RAM:08x}
    lsls    r4, r0, #4
    subs    r4, r4, r0              /* channel x 15 */
    adds    r4, r4, r1
    add.w   r4, r3, r4, lsl #1
    strh    r2, [r4, #{KEEP_VALS_OFF}]  /* the value first ... */
    add.w   r4, r3, r0, lsl #1
    ldrh    r0, [r4, #{KEEP_BITS_OFF}]
    movs    r2, #1
    lsls    r2, r1
    orrs    r0, r2
    strh    r0, [r4, #{KEEP_BITS_OFF}]  /* ... then the touched bit */
pmidi_out:
    msr     apsr_nzcvq, r12
    pop     {{r0, r1, r2, r3, r4, r12}}
pmidi_next:
    .short  0, 0

pblock:                             /* block start 0x08093e72; r0 = profiler */
    push    {{r0, r1, r2, r3, r4, r5, r6, r7, r12, lr}}
    mrs     r12, apsr
    push    {{r12}}
    sub     sp, #28                 /* the event, as the dispatcher builds it */
    ldr     r4, =0x{KEEP_RAM:08x}
    movs    r0, #0
    str     r0, [r4, #{KEEP_REPLAY_OFF}]   /* last block's replays were dispatched */
    ldr     r0, [r4, #{KEEP_MODE_OFF}]
    ldr     r1, =0x{KEEP_ON:08x}
    cmp     r0, r1
    bne     pb_out
    ldr     r0, [r4, #{KEEP_LOAD_OFF}]
    ldr     r1, [r4, #{KEEP_DONE_OFF}]
    cmp     r0, r1
    beq     pb_have
    str     r0, [r4, #{KEEP_DONE_OFF}]     /* a new load: from the top */
    movs    r1, #0
    str     r1, [r4, #{KEEP_IDX_OFF}]
    str     r1, [r4, #{KEEP_STABLE_OFF}]
    str     r1, [r4, #{KEEP_AGE_OFF}]
pb_have:
    ldr     r5, [r4, #{KEEP_IDX_OFF}]
    cmp     r5, #{KEEP_SLOTS}
    bhs     pb_out
    ldr     r0, [r4, #{KEEP_AGE_OFF}]
    adds    r0, #1
    str     r0, [r4, #{KEEP_AGE_OFF}]   /* blocks since the load finished */
    ldr     r7, =0x{MANAGER:08x}
    movw    r0, #0x{ENG_READY:x}
    ldrb    r0, [r7, r0]
    cmp     r0, #0
    beq     pb_unsteady             /* still loading */
    movw    r0, #0x{ENG_X:x}
    ldr     r6, [r7, r0]
    sub.w   r0, r6, #0x24000000
    cmp.w   r0, #0x50000
    bhs     pb_unsteady             /* no engine object (yet) */
    movw    r0, #0x{RING_R:x}
    ldr     r0, [r6, r0]
    movw    r1, #0x{RING_W:x}
    ldr     r1, [r6, r1]
    cmp     r0, r1
    bne     pb_unsteady             /* the load's parameters not all applied yet */
    movw    r0, #0x{LOADER_BUSY:x}      /* the firmware's own loader-idle test, */
    ldr     r0, [r7, r0]            /* 0x08097bdc: +0x973c == 0 ... */
    cbnz    r0, pb_busy
    movw    r0, #0x{LOADER_R:x}
    ldr     r0, [r7, r0]
    movw    r1, #0x{LOADER_W:x}
    ldr     r1, [r7, r1]
    cmp     r0, r1                  /* ... and its ring +0x83d4 == +0x83d8 */
    bne     pb_busy
    movw    r0, #0x{REPLY_R:x}
    ldr     r0, [r7, r0]
    movw    r1, #0x{REPLY_W:x}
    ldr     r1, [r7, r1]
    cmp     r0, r1                  /* and nothing waiting for the UI (+0x89e0/4) */
    bne     pb_busy
    ldr     r0, [r4, #{KEEP_STABLE_OFF}]
    adds    r0, #1
    str     r0, [r4, #{KEEP_STABLE_OFF}]
    cmp     r0, #{KEEP_STEADY_BLOCKS}
    blo     pb_out                  /* idle, but not for long enough yet */
    b       pb_ready
pb_busy:
    movs    r0, #0
    str     r0, [r4, #{KEEP_STABLE_OFF}]
    ldr     r0, [r4, #{KEEP_AGE_OFF}]
    movw    r1, #{KEEP_TIMEOUT_BLOCKS}
    cmp     r0, r1
    blo     pb_out                  /* the loader is busy: wait (up to the timeout) */
    b       pb_ready
pb_unsteady:
    movs    r0, #0
    str     r0, [r4, #{KEEP_STABLE_OFF}]
    b       pb_out                  /* never without a ready engine and an empty ring */
pb_ready:
    movw    r0, #0x{QUEUE_COUNT & 0xffff:x}
    movt    r0, #0x{QUEUE_COUNT >> 16:x}
    ldr     r0, [r6, r0]
    cmp     r0, #{KEEP_PER_BLOCK}
    bhi     pb_out                  /* leave room for live CCs */
    movs    r7, #{KEEP_PER_BLOCK}
pb_loop:
    cmp     r5, #{KEEP_SLOTS}
    bhs     pb_save
    movs    r0, #{KEEP_CC_COUNT}
    udiv    r1, r5, r0              /* channel */
    mls     r2, r1, r0, r5          /* CC - 79 */
    add.w   r3, r4, r1, lsl #1
    ldrh    r3, [r3, #{KEEP_BITS_OFF}]
    lsrs    r3, r2
    lsls    r3, r3, #31
    beq     pb_next                 /* never touched: the preset's value stands */
    movs    r0, #7                  /* type 7, as 0x08093eee: a CC */
    str     r0, [sp]
    movs    r0, #0
    str     r0, [sp, #4]
    str     r0, [sp, #20]
    str     r1, [sp, #8]
    adds    r0, r2, #{KEEP_CC_FIRST}
    str     r0, [sp, #12]
    add.w   r0, r4, r5, lsl #1
    ldrh    r0, [r0, #{KEEP_VALS_OFF}]
    str     r0, [sp, #16]
    ldr     r0, =0x{KEEP_REPLAY_MAGIC:08x}
    str     r0, [r4, #{KEEP_REPLAY_OFF}]
    mov     r0, r6
    mov     r1, sp
pb_post:
    .short  0, 0                    /* bl 0x080950c0: the dispatcher's own post */
    adds    r5, #1
    subs    r7, #1
    bne     pb_loop
    b       pb_save
pb_next:
    adds    r5, #1
    b       pb_loop
pb_save:
    str     r5, [r4, #{KEEP_IDX_OFF}]
pb_out:
    add     sp, #28
    pop     {{r12}}
    msr     apsr_nzcvq, r12
    pop     {{r0, r1, r2, r3, r4, r5, r6, r7, r12, lr}}
pblock_next:
    .short  0, 0
    .balign 4
    .ltorg
pvalues:
    .word   0x080c0030, 0x080c0034  /* stock OFF / ON */
    .balign 4
plabel:
    .asciz  "Keep CCs:"
    .balign 4
pxml:
    .asciz  "keepccs"
    .balign 4
"""
    code, syms = assemble("hookP", src)
    code = bytearray(code)
    for sym, target, call in (
        ("preg_call", 0x08069e7c, True),
        ("preg_next", nexts["preg"], False),
        ("pstore_call", 0x0808951e, True),
        ("pstore_next", nexts["pstore"], False),
        ("ppost_set", KEEP_SETPARAM, True),
        ("ppost_next", nexts["ppost"], False),
        ("pfinal_call", FINALIZE_FN, True),
        ("pctor_next", nexts["pctor"], False),
        ("pmidi_next", nexts["pmidi"], False),
        ("pb_post", MIDI_POST_FN, True),
        ("pmidi_stock", MIDI_POST_FN, False),
        ("pblock_next", nexts["pblock"], False),
    ):
        off = syms[sym]
        code[off:off + 4] = enc_b_bl(org + off, target, call)
    return bytes(code), syms


KEEP_CALLS = (                       # (site, entry, what): head insertions
    (0x0806abbc, "preg", "the uiflip registration"),
    (0x08089efe, "pstore", "the global store's 0xdc entry"),
    (0x0807c5be, "ppost", "the Settings apply-all post"),
    (0x0807d51c, "pedit", "the Settings live-edit post"),
    (MIDI_POST_CALL, "pmidi", "the MIDI CC post"),
    (0x08093e72, "pblock", "the block start"),
)


def apply_keep_ccs(d, j_org, j_syms):
    """Guard P's facts, append P, take each site at the head of its chain."""
    for va, raw, what in KEEP_GUARDS:
        _guard(d, va, raw, what)
    for va in (0x08042488, 0x0807c538, 0x0807fa74):
        if struct.unpack_from("<I", d, va - BASE)[0] != MANAGER:
            refuse(f"The engine literal at {va:#x} is not where Keep CCs expects it.")
    for site in KEEP_FINAL_SITES:
        if branch_target(d, site) != FINALIZE_FN:
            refuse(f"The preset-load finish at {site:#x} is not what Keep CCs expects.")
    nexts = {}
    for site, entry, what in KEEP_CALLS:
        nexts[entry] = branch_target(d, site)
    if nexts.pop("pedit") != nexts["ppost"]:   # pedit falls into ppost
        refuse("The two Settings post sites no longer lead to the same place.")
    at = j_org + j_syms["jctor_next"]
    nexts["pctor"] = branch_target(d, at)
    org = BASE + len(d)
    code, syms = build_keep_hook(org, nexts)
    org2 = _append(d, bytearray(code))
    assert org2 == org
    for site, entry, _ in KEEP_CALLS:
        d[site - BASE:site - BASE + 4] = enc_b_bl(site, org + syms[entry], True)
    for site in KEEP_FINAL_SITES:
        d[site - BASE:site - BASE + 4] = enc_b_bl(site, org + syms["pfinal"], True)
    d[at - BASE:at - BASE + 4] = enc_b_bl(at, org + syms["pctor"], False)
    vprint(f"P Keep CCs {len(code)} bytes at {org:#010x}, id {KEEP_ID:#x}, ram {KEEP_RAM:#x}")
    return org, syms


def write_slot(d, va, expect, new, display_max=None):
    """Overwrite one 16-byte string slot, guarding on its current contents.

    Same fail-safe discipline as the code hooks: refuse rather than clobber
    something unrecognised. Null-pads the whole slot so no tail of the old
    string can survive past the new terminator.

    `display_max` is a SECOND, tighter limit for slots whose contents have to
    stay legible on the module's own screen rather than merely fit in flash.
    Storage and legibility are different questions and this function now asks
    both. See VER_DISPLAY_MAX.
    """
    o = va - BASE
    cur = bytes(d[o:o + SLOT]).split(b"\0")[0].decode()
    if cur != expect:
        refuse(f"The splash text at {va:#x} is not what it should be.",
               f"Found {cur!r}, expected {expect!r}.",
               "",
               "This firmware is not the build this patcher was made for.")
    if len(new) >= SLOT:
        refuse(f"{new!r} is too long for the splash screen.",
               f"It is {len(new)} characters and the limit is {SLOT - 1}.",
               "",
               "There is a pointer table straight after this text, so a longer",
               "string would overwrite something the module needs.")
    if display_max is not None and len(new) > display_max and not LEGACY_VERSION:
        refuse(f"{new!r} will not be legible on the module's splash screen.",
               f"It is {len(new)} characters and the display limit is "
               f"{display_max}.",
               "",
               "It would FIT in the slot -- storage allows 15 -- but the panel",
               "shows fewer, so it would be silently truncated and you would",
               "not know which image is on the card. `2.3.7-mod-b10` came back",
               "off the module as `2.3.7-mod-b1` and two hardware faults were",
               "very nearly recorded against the wrong build.",
               "",
               f"Use a shorter form, e.g. {new.replace('-mod', '')!r}.",
               "",
               "If you are deliberately REBUILDING A HISTORICAL IMAGE to check",
               "its fingerprint, pass --legacy-version to lift this check. The",
               "15-character storage limit still applies.")
    d[o:o + SLOT] = new.encode().ljust(SLOT, b"\0")
    vprint(f"splash {va:#x} {cur!r} -> {new!r}")


# --- The stock UNCACHED POOL, and where patch RAM may live ----------------------
# 0x08046ba4 is a top-down bump allocator over the two uncached MPU regions
# (0x24060000-0x2407ffff): its pointer is the .data word 0x240000b4, which the
# startup copy fills from 0x080e3e4c with 0x24080000; its running total is
# 0x24009e34 (.bss), capped at 0x20000; requests round up to 32 bytes; nothing
# is ever freed ("Insufficient memory for request %d" past the cap). Exactly
# eleven call sites (no pointer to it, no tail call), each a constant size,
# each run once. Replayed in boot order (tests/test_firmware_memory.py):
#
#   0x2407e000-0x24080000  8192  SAI audio DMA buffer (+0x440), 0x08045b1c
#   0x2407c000-0x2407e000  8192  SAI audio DMA buffer (+0x448)
#   0x2407bdc0-0x2407c000   576  FatFs volume object (sector window), 0x08042efc
#   0x2407bcc0-0x2407bdc0   256  0x080422e0's +0x608
#   0x2406f8c0-0x2407bcc0 50176  engine +0x4f60 = PresetMgr +0x2b0: preset.xml is
#                                read and parsed in it (0x0805bed8 at 0x0808ce30),
#                                samples stream through it (0x08097d5c)
#   0x2406f6c0-0x2406f8c0   512  0x080a26b8's singleton
#   0x2406f4e0-0x2406f6c0   480  five 0x58 objects (engine, PresetMgr x2, two singletons)
#
# Up to ch11 the Character's slots 1-3 and the OSD's UI-side area lived inside
# that 50 KB buffer, so live slots corrupted preset loads ("New Preset", some
# pads silent) and file data corrupted the slots (a tone, a hiccup). Patch RAM
# must sit in [STACK_TOP, POOL_FLOOR): above the main stack, below the pool.
POOL_ALLOC = 0x08046ba4
POOL_ALLOC_CODE = ("08b510f01f0f03d020f01f0101f12000094b1b680344b3f5003f06d8074a1168"
                   "081a1060044a136008bd01460448fcf7f1f80020f8e700bf349e0024b4000024"
                   "f0f40b08")      # the whole function and its pool: cap 0x20000, vars, message
POOL_TOP_INIT_VA, POOL_TOP_INIT = 0x080e3e4c, 0x24080000   # .data image of 0x240000b4
POOL_CAP = 0x20000
POOL_CALLS = (                       # (first instruction, its bytes through the bl, size)
    (0x08045b22, "4ff4005001f03df8", 0x2000),          # SAI buffer
    (0x08045b38, "4ff4005001f032f8", 0x2000),          # SAI buffer
    (0x08042f28, "4ff40f7003f03afe", 0x23c),           # FatFs volume
    (0x0804231c, "4ff4807004f040fc", 0x100),
    (0x08097598, "4ff4444044f6d429aff700fb", 0xc400),  # the preset/stream buffer
    (0x080a26d0, "4ff40070a4f766fa", 0x200),
    (0x0809764e, "582045f69c74aff7a6fa", 0x58),
    (0x0808b486, "5820bbf78cfb", 0x58),
    (0x0808b490, "5820bbf787fb", 0x58),
    (0x080aa816, "58209cf7c4f9", 0x58),
    (0x0808ed02, "5820b7f74eff", 0x58),
)
POOL_FLOOR = POOL_TOP_INIT - sum((n + 0x1f) & ~0x1f for _, _, n in POOL_CALLS)   # 0x2406f4e0
assert POOL_FLOOR == 0x2406f4e0 and POOL_TOP_INIT - POOL_FLOOR <= POOL_CAP


def patch_ram_ranges():
    """Every byte of RAM any hook in this file writes: (what, lo, hi)."""
    return [
        ("D/E compressor store", PATCH_RAM, PATCH_RAM + PATCH_SUM + 4),
        ("F/G EQ store", EQ_STORE, EQ_STORE_END),
        ("H Program Change", PC_STATE, PC_STATE + 8),
        ("J Clk Loss", CLK_MODE, CLK_HELD + 4),
        ("K CPU meter", K_MODE, K_HOLD + 4),
        ("L Preset BPM", L_CLOCK, L_PEND + 4),
        ("N store and engine", CHR_RAM, CHR_RAM + 0x100),
        ("O OSD state", OSD_RAM_OFF, OSD_RAM_OFF + 0xb4),
        ("N dry buffers", CHR_WORK_DEFAULT, CHR_WORK_DEFAULT + 0x800),
        *((f"N slot {k}", CHR_WORK_DEFAULT + o, CHR_WORK_DEFAULT + o + n)
          for k, (o, n) in enumerate(CHR_SLOTS)),
        ("O UI-side area", OSD2_RAM, OSD2_RAM + OSD2_BYTES),
        ("P Keep CCs", KEEP_RAM, KEEP_RAM + KEEP_BYTES),
        ("Dr auto makeup flag", AUTO_RAM, AUTO_RAM + 8),
    ]


def pool_callers(d):
    """Every bl/b.w to the pool allocator in the stock part of the image."""
    out = []
    for o in range(0, STOCK_SIZE - 4, 2):
        hw1, hw2 = struct.unpack_from("<HH", d, o)
        if hw1 & 0xf800 == 0xf000 and hw2 & 0xd000 in (0xd000, 0x9000):   # bl, b.w
            if branch_target(d, BASE + o) == POOL_ALLOC:
                out.append(BASE + o)
    return out


def check_patch_ram(d):
    """Refuse unless the stock pool is exactly as analysed and every byte of
    patch RAM sits above the main stack and below the pool's floor."""
    _guard(d, POOL_ALLOC, POOL_ALLOC_CODE, "uncached pool allocator")
    if struct.unpack_from("<I", d, POOL_TOP_INIT_VA - BASE)[0] != POOL_TOP_INIT:
        refuse("The uncached pool's top is not where the patcher expects it.")
    sites = []
    for va, raw, _ in POOL_CALLS:
        _guard(d, va, raw, "uncached pool request")
        sites.append(va + len(raw) // 2 - 4)
    if sorted(pool_callers(d)) != sorted(sites):
        refuse("The uncached pool has callers the patcher has not accounted for.")
    rs = sorted(patch_ram_ranges(), key=lambda r: r[1])
    for what, lo, hi in rs:
        if lo < STACK_TOP or hi > POOL_FLOOR:
            refuse(f"Patch RAM ({what}, {lo:#x}-{hi:#x}) is outside {STACK_TOP:#x}-{POOL_FLOOR:#x}.",
                   "", "Below is the firmware's main stack; above is its uncached pool,",
                   "where it reads presets and streams samples.")
    for (w1, _, h1), (w2, l2, _) in zip(rs, rs[1:]):
        if l2 < h1:
            refuse(f"Patch RAM overlaps: {w1} and {w2}.")
    vprint(f"patch RAM {rs[0][1]:#x}-{max(h for _, _, h in rs):#x}, "
           f"stack top {STACK_TOP:#x}, pool floor {POOL_FLOOR:#x}")


def main():
    global REASSEMBLE, VERBOSE, LEGACY_VERSION
    flags = sys.argv[1:]
    REASSEMBLE = "--reassemble" in flags
    VERBOSE = "-v" in flags or "--verbose" in flags
    LEGACY_VERSION = "--legacy-version" in flags
    argv = [a for a in flags if not a.startswith("-")]

    if len(argv) < 2:
        sys.exit(USAGE)

    inp, outp = argv[0], argv[1]
    ver = argv[2] if len(argv) > 2 else "2.3.9-mod"
    # The default output is 2.3.9-mod: everything below (Preset BPM, the
    # Character chain, the OSD with the CPU bar, Keep CCs, auto makeup...).
    # --v239 says the same thing explicitly. --v238 rebuilds the published
    # 2.3.8-mod exactly. TEST_FLAGS build single features on the 2.3.8 base.
    v238 = "--v238" in flags
    test_build = any(f in flags for f in TEST_FLAGS)
    if v238 and ("--v239" in flags or test_build):
        refuse("--v238 rebuilds the published 2.3.8-mod exactly: no other options.")
    if "--v239" in flags and test_build:
        refuse("2.3.9-mod is the release set: no test options with it.")
    v239 = not (v238 or test_build)
    cpu_meter = v239 or "--cpu-meter" in flags     # opt-in test build, never a release
    preset_bpm = v239 or "--preset-bpm" in flags   # opt-in test build, never a release
    clock_debug = "--clock-debug" in flags # diagnostic build, needs --preset-bpm
    character = v239 or "--character" in flags     # opt-in test build, never a release
    osd = v239 or "--osd" in flags                 # opt-in test build, needs --character
    if "--fast-ram" in flags:
        refuse("--fast-ram was removed in 2.3.9-ch12.",
               "It moved the Character's state to D2 SRAM and was never tested on",
               "the module. The state now lives in checked patch RAM.")
    if osd and not character:
        refuse("--osd needs --character: the OSD's MIDI side lives in its C object.")
    if len(argv) <= 2:
        if v239:
            ver = RELEASE_VERSION
        elif v238:
            ver = PREVIOUS_VERSION
        elif osd:
            ver = OSD_VERSION
        elif character:
            ver = CHARACTER_VERSION
        elif cpu_meter and preset_bpm:
            ver = "2.3.9-test"
        elif cpu_meter:
            ver = CPU_METER_VERSION
        elif preset_bpm:
            ver = "2.3.9-dbg1" if clock_debug else PRESET_BPM_VERSION
        elif clock_debug:
            ver = "2.3.8-dbg"
    # Program Change is standard from 2.3.8-mod. The flag is still accepted
    # so the beta's documented command keeps working; it changes nothing.

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:                      # ancient Python, or an odd console
        pass

    # 1010music ship MICRO234.zip containing a plain MICRO.BIN, and the module
    # requires the patched image be called MICRO.BIN too -- so the obvious
    # command overwrites the only stock copy, which is the one thing you cannot
    # get back except by downloading again.
    if os.path.realpath(inp) == os.path.realpath(outp):
        refuse("The input and the output are the same file.",
               f"{inp}",
               "",
               "That would overwrite your stock firmware, and it is the only",
               "way back to a working module if anything goes wrong.",
               "",
               "Write the patched image somewhere else and copy it to the card:",
               "",
               "    mkdir patched",
               f"    {PY} patch_micro.py {inp} patched/MICRO.BIN")

    print("\n  Checking your firmware...")
    try:
        d = bytearray(open(inp, "rb").read())
    except OSError as e:
        refuse(f"Could not open {inp}", str(e), "",
               "Check the path. If the name has spaces in it, put it in quotes.")

    sha = hashlib.sha256(d).hexdigest()
    if len(d) != STOCK_SIZE or sha != STOCK_SHA:
        refuse("That is not the stock 2.3.4 firmware.",
               f"expected  {STOCK_SIZE:,} bytes   {STOCK_SHA[:24]}...",
               f"got       {len(d):,} bytes   {sha[:24]}...",
               "",
               "Usually this means one of:",
               "  - it is a different firmware version",
               "  - it has already been patched",
               "  - the download did not finish properly",
               "",
               "Fresh copy: https://1010music.com/downloads")
    print(f"     ok   Genuine bitbox micro 2.3.4, nothing done to it yet")
    vprint(f"sha256 {sha}")

    for label, hook, ret, vslot, arms in HOOKS:
        cur = bytes(d[hook - BASE:hook - BASE + 4])
        want = enc_bne_w(hook, ret)
        if cur != want:
            refuse(f"The code at {hook:#x} is not what it should be.",
                   f"found {cur.hex()}, expected {want.hex()}",
                   "",
                   "The file is the right size and checksum but the machine code",
                   "is wrong, which should be impossible. Do not try to force it.")
        vprint(f"hook {label:16} {hook:#x} = bne.w {ret:#x} ({cur.hex()})")

    # The granular arms branch into the middle of a function rather than to an
    # entry point, so check each landing site still holds the instruction it
    # held in 2.3.4 before trusting the address.
    for cc, (va, expect) in sorted(CASES.items()):
        got, = struct.unpack_from("<I", d, va - BASE)
        if got != expect:
            refuse(f"The granular code at {va:#x} is not what it should be.",
                   f"found {got:#010x}, expected {expect:#010x}  (CC{cc})",
                   "",
                   "This is where the patch jumps to let the firmware apply the",
                   "value itself. Landing on the wrong instruction would corrupt",
                   "the pad, so nothing has been written.")
        vprint(f"CC{cc} case {va:#x} = {got:#010x}")
    for nm, va, expect in (("stop A", STOP_A, STOP_A_GUARD),
                           ("stop B", STOP_B, STOP_B_GUARD),
                           # Called, not jumped into, but the same reasoning:
                           # the wrong address here is a call into the middle
                           # of something with four arguments behind it.
                           ("class B set-parameter",
                            CLASSB_SETPARAM, CLASSB_SETPARAM_GUARD),
                           # Proof the delay really does handle Filter and
                           # Width, and at these ids -- the decompiler missed
                           # this stretch, so the disassembly is the evidence.
                           ("delay filter/width",
                            DLYFX_GUARD_VA, DLYFX_GUARD),
                           # The compressor's fields are in RAM, so these four
                           # are the only way to check them: each is the
                           # instruction or literal that computes an address
                           # the patch is about to write to.
                           ("compressor dispatch", COMP_DISP_VA, COMP_DISP),
                           ("compressor on/off offset", COMP_FLAG_VA, COMP_FLAG),
                           ("compressor field offset", COMP_INIT_VA, COMP_INIT),
                           ("compressor default", COMP_DFLT_VA, COMP_DFLT),
                           ("session pointer", COMP_SESS_VA, COMP_SESS),
                           ("compressor DSP call", COMP_DSP_CALL, COMP_DSP_CALL_GUARD),
                           ("EQ vtable handler slot", EQ_VT_GUARD_VA, EQ_VT_GUARD),
                           ("EQ vtable store", EQ_CTOR_VA, EQ_CTOR_GUARD),
                           ("EQ designer entry", EQ_DSGN_VA, EQ_DSGN_GUARD),
                           ("EQ coefficient slot", EQ_COEF_VA, EQ_COEF_GUARD),
                           ("EQ frequency is in Hz", EQ_KONST_VA, EQ_KONST),
                           ("EQ handler audio fetch", EQ_HAND_VA, EQ_HAND_GUARD),
                           ("EQ gain is in dB", EQ_GAINK_VA, EQ_GAINK),
                           ("heap start", HEAP_LO_VA, HEAP_LO),
                           ("heap end", HEAP_HI_VA, HEAP_HI),
                           ("initial stack pointer", STACK_VA, STACK_TOP)):
        got, = struct.unpack_from("<I", d, va - BASE)
        if got != expect:
            refuse(f"The {nm} code at {va:#x} is not what it should be.",
                   f"found {got:#010x}, expected {expect:#010x}")
        vprint(f"{nm} {va:#x} = {got:#010x}")

    print("     ok   The code is where the patch expects to find it")

    print("\n  Embellishing your bitbox micro with community requests...")

    for label, hook, ret, vslot, arms in HOOKS:
        org = BASE + len(d)
        assert org % 4 == 0
        code = build(org, ret, vslot, arms, label[0])
        if len(code) % 4:                      # keep the next block 4-byte aligned
            code += b"\x00" * (4 - len(code) % 4)
        d += code
        d[hook - BASE:hook - BASE + 4] = enc_bne_w(hook, org)
        def _o(v):
            if isinstance(v, tuple):
                return "/".join(_o(x) for x in v)
            return "-" if v is None else f"{v:#x}"
        detail = ", ".join(f"CC{c}->{k}+{_o(o)}" for c, k, o in arms)
        vprint(f"{label:16} {len(code):3} bytes at {org:#010x}   {detail}")

    # --- third hook: the MIDI dispatcher's CC-event post ------------------
    off = MIDI_POST_CALL - BASE
    cur = bytes(d[off:off + 4])
    want = enc_b_bl(MIDI_POST_CALL, MIDI_POST_FN, True)
    if cur != want:
        refuse(f"The MIDI code at {MIDI_POST_CALL:#x} is not what it should be.",
               f"found {cur.hex()}, expected {want.hex()}",
               "",
               "Do not try to force it.")
    vprint(f"hook C midi post {MIDI_POST_CALL:#x} = bl {MIDI_POST_FN:#x} ({cur.hex()})")
    org = BASE + len(d)
    assert org % 4 == 0
    code = build_midi_hook(org)
    if len(code) % 4:
        code += b"\x00" * (4 - len(code) % 4)
    d += code
    hookc_org = org
    vprint(f"{'C delay FX':16} {len(code):3} bytes at {org:#010x}   "
           f"CC{CC_BEATSYNC}->beatsync(0x17), CC{CC_PINGPONG}->pingpong(0x18), "
           f"CC{CC_DLYFILTER}->filtenable(0xab), CC{CC_DLYWIDTH}->filtquality(0xac) "
           f"@ key {DELAY_KEY:#x}")

    # --- fourth block: the compressor, chained in FRONT of hook C ---------
    org = BASE + len(d)
    assert org % 4 == 0
    code = build_comp_hook(org, hookc_org, full_range=v239)
    if len(code) % 4:
        code += b"\x00" * (4 - len(code) % 4)
    d += code
    d[off:off + 4] = enc_b_bl(MIDI_POST_CALL, org, True)
    # Capture hook D's origin HERE, next to the block that built it, exactly as
    # hook C does above. The fifth block reassigns `org` before hook F is built,
    # so reading `org` down there yields hook E -- the compressor's audio-thread
    # DSP hook -- and hook F's `fnext` then branches the MIDI thread straight
    # into it. It faults on the first CC of any number, on any channel, because
    # every path through hook F converges on `fout`. That shipped as b1.
    hookd_org = org
    vprint(f"{'D compressor CC':16} {len(code):3} bytes at {org:#010x}   "
           f"CC{CC_COMP_ONOFF}->on/off +{COMP_ONOFF_OFF:#x} (session by proof), "
           f"CC{CC_COMP_FIRST}-{CC_COMP_FIRST + CC_COMP_COUNT - 1}->store @{PATCH_RAM:#x}, "
           f"then -> hook C {hookc_org:#010x}")

    # --- fifth block: stamp the store onto the live compressor each block ---
    off_e = COMP_DSP_CALL - BASE
    cur = bytes(d[off_e:off_e + 4])
    want = enc_b_bl(COMP_DSP_CALL, COMP_DSP_FN, True)
    if cur != want:
        refuse(f"The compressor call at {COMP_DSP_CALL:#x} is not what it should be.",
               f"found {cur.hex()}, expected {want.hex()}")
    org = BASE + len(d)
    assert org % 4 == 0
    code = build_comp_dsp_hook(org)
    if len(code) % 4:
        code += b"\x00" * (4 - len(code) % 4)
    d += code
    d[off_e:off_e + 4] = enc_b_bl(COMP_DSP_CALL, org, True)
    vprint(f"{'E compressor DSP':16} {len(code):3} bytes at {org:#010x}   "
           f"store {PATCH_RAM:#x} -> live object, then -> DSP {COMP_DSP_FN:#010x}")

    # --- sixth block: the EQ's CC arm, chained in FRONT of hook D ---------
    # `hookd_org` was captured beside the fourth block. Do NOT read `org` here:
    # it holds hook E by this point. The guard below refuses the whole class --
    # a chain target that is not the hook the comment names is a silent,
    # cross-thread wild branch, and the only symptom is a reboot on any MIDI.
    eq_chain_targets = {"hook D": hookd_org, "hook C": hookc_org}
    assert hookd_org != org, (
        f"hook F would chain to hook E ({org:#010x}), not hook D -- this is the "
        f"b1 defect: the MIDI thread branching into the compressor's audio-thread "
        f"DSP hook, faulting on the first CC of any number")
    assert len(set(eq_chain_targets.values())) == len(eq_chain_targets), \
        "two chain targets resolved to the same address -- one of them is wrong"
    org = BASE + len(d)
    assert org % 4 == 0
    code = build_eq_midi_hook(org, hookd_org)
    if len(code) % 4:
        code += b"\x00" * (4 - len(code) % 4)
    d += code
    d[off:off + 4] = enc_b_bl(MIDI_POST_CALL, org, True)
    vprint(f"{'F EQ CC':16} {len(code):3} bytes at {org:#010x}   "
           f"CC{CC_EQ_MODE}->mode, "
           f"CC{CC_EQ_A}/{CC_EQ_B}/{CC_EQ_C}->control A/B/C, "
           f"CC{CC_EQ_BTN_A}/{CC_EQ_BTN_B}/{CC_EQ_BTN_C}->button A/B/C, "
           f"CC{CC_EQ_BYPASS}->master bypass, CC{CC_EQ_SLOPE}->slope, "
           f"store @{EQ_STORE:#x}, then -> hook D {hookd_org:#010x}")

    # --- seventh block: the EQ arm itself, installed in the class vtable ---
    off_g = EQ_VT_HANDLER - BASE
    cur = struct.unpack("<I", bytes(d[off_g:off_g + 4]))[0]
    if cur != EQ_VT_GUARD:
        refuse(f"The EQ vtable slot at {EQ_VT_HANDLER:#x} is not what it should be.",
               f"found {cur:#010x}, expected {EQ_VT_GUARD:#010x}")
    org = BASE + len(d)
    assert org % 4 == 0
    code = build_eq_dsp_hook(org)
    if len(code) % 4:
        code += b"\x00" * (4 - len(code) % 4)
    d += code
    d[off_g:off_g + 4] = struct.pack("<I", org | 1)
    vprint(f"{'G EQ DSP':16} {len(code):3} bytes at {org:#010x}   "
           f"vtable {EQ_VT_HANDLER:#010x} -> arm, then -> stock handler {EQ_HANDLER:#010x}")

    for what, cc in FEATURES:
        print(f"     ok   {what:<28} {cc}")
    if v239:
        print(f"     ok   {'Compressor auto makeup, ch 1':<28} CC 46")

    check_patch_ram(d)
    apply_program_change(d)
    rows = [0xc3, 0xb5, CLK_ID, 0x17b, 0x78]
    if preset_bpm:
        rows.insert(rows.index(CLK_ID) + 1, 0x66)   # right after Clk Loss
    if cpu_meter and not v239:          # rc1: the bar is a mode of the OSD row
        rows += [CPU_ID_METER]
    if osd:
        rows.append(OSD_ID)
    if v239:
        rows.append(KEEP_ID)
    if len(rows) > 8:
        refuse("Settings page 2 has eight rows; this combination of options needs "
               f"{len(rows)}.")
    apply_settings_page(d, rows)
    apply_flip_ui(d)
    j_org, j_syms = apply_clock_hold(d)
    l_entries = ()
    if preset_bpm:
        l_org, l_syms = apply_preset_bpm(d, j_org, j_syms)
        l_entries = (l_org + l_syms["lpost"], l_org + l_syms["ltick"],
                     l_org + l_syms["lblock"])
        print("     ok   Preset BPM, HOLD capture     TOOLS > Settings"
              + ("" if v239 else " (TEST BUILD)"))
    if clock_debug:
        apply_clock_debug(d, l_entries, j_org, j_syms, own_ctor=not preset_bpm)
        print("     ok   Clock diagnostic strips      (DEBUG BUILD)")
    if cpu_meter:
        m_entries = (apply_clock_debug.entry,) if clock_debug else ()
        apply_cpu_meter(d, j_org, j_syms, chained=l_entries + m_entries, row=not v239)
        if not v239:
            print("     ok   CPU Meter                    TOOLS > Settings (TEST BUILD)")
    if character:
        n_org, n_syms = apply_character(d, CHR_FEATURE_AUTO if v239 else 0)
        print(f"     ok   Character chain, ch 1        CC {CHR_CC_FIRST}-{CHR_CC_LAST}"
              + ("" if v239 else " (TEST BUILD)"))
        if osd:
            apply_osd(d, n_org, n_syms, j_org, j_syms, modes=v239)
            if v239:
                print("     ok   OSD: pop-ups and CPU bar     TOOLS > Settings")
            else:
                print("     ok   OSD pop-up and OSD: row     TOOLS > Settings (TEST BUILD)")
    if v239:
        if not osd:
            refuse("Keep CCs needs the OSD's release variant (its pad recorders).")
        apply_keep_ccs(d, j_org, j_syms)
        print("     ok   Keep CCs: OFF/ON             TOOLS > Settings")
    for what, where in SETTINGS_FEATURES:
        print(f"     ok   {what:<28} {where}")

    # Splash: drop 1010music's attribution so the modifications are not
    # credited to them, and mark the version as a modified build.
    write_slot(d, ATTRIB_VA, ATTRIB_STOCK, ATTRIB_NEW)
    write_slot(d, VER_VA, VER_STOCK, ver, display_max=VER_DISPLAY_MAX)

    print(f"     ok   Splash screen, so nobody blames 1010music for my work")

    # The bootloader programs and then verifies the image, and an image whose
    # length is not a multiple of four fails that check ("Unable to verify").
    # Every block above pads itself; this is the backstop.
    if len(d) % 4:
        refuse(f"The patched image is {len(d):,} bytes, not a multiple of 4.",
               "",
               "The module's bootloader would program it and then fail to",
               "verify it. Nothing has been written.")

    try:
        open(outp, "wb").write(bytes(d))
    except OSError as e:
        refuse(f"Could not write {outp}", str(e))

    out_sha = hashlib.sha256(bytes(d)).hexdigest()
    print(f"\n  Done. {outp} is ready -- {len(d):,} bytes.\n")
    print(f"     Fingerprint  {out_sha}")
    print( "     If that matches the one in the install guide, you have built exactly")
    print( "     the same firmware as everybody else, down to the byte.")

    print("\n  Getting it onto the module:\n")
    print( "     1.  Copy the file to the root of a microSD card, named")
    print(f"         MICRO.BIN -- capitals, and not inside a folder")
    print( "     2.  Power the module off and put the card in")
    print( "     3.  Hold the white right-arrow button, then power on")
    print( "     4.  Let go when it says Erasing. Takes about 15 seconds.")
    print(f"\n     You will know it worked when the splash reads")
    print(f"     'bitbox micro / {ATTRIB_NEW} / {ver}'.")

    print("\n  Enjoy. If anything misbehaves, put the stock firmware back")
    print("  before you go and tell 1010music about it -- this bit is on us.\n")


if __name__ == "__main__":
    main()
