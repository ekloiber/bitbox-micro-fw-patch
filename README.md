# Bitbox Micro: Gate Record

This fork of
[modularpatcher/bitbox-micro-fw-patch](https://github.com/modularpatcher/bitbox-micro-fw-patch)
turns the Bitbox Micro's input jacks into a looper's foot switches. A gate can
record into a pad, clear it, or do both, with no MIDI controller needed.

It is unofficial. Nobody at 1010music, or at the original project, wrote or
endorsed this fork's feature, so do not ask either of them for support.

## What It Adds

Three rows at the end of the Settings screen (press TOOLS). Each one is OFF,
or one of the input jacks IN 1 to IN 8.

| Row | What a gate on the chosen jack does |
|---|---|
| **Gate Rec** | Records into the selected pad, if it is empty. A second gate ends the take. |
| **Gate Clr** | Clears the selected pad. The WAV file stays on the card. |
| **Gate Loop** | Clears the selected pad and records into it. One gate records an empty pad, replaces a loaded one with a new take, or ends the take in progress. |

These gates do exactly what MIDI Rec's action notes do (103 record, 104 clear),
so the manual's MIDI Triggered Recording section describes the behaviour. The
pad's own record settings apply: Rec Input, Rec Quant, Length and RecToPlay.
MIDI Rec does not need to be on.

A jack you assign to a row stops triggering its own pad. Every other jack works
exactly as stock. The settings are saved, so they survive power cycles.

## Using It as a Looper

1. On the Input screen, set inputs 1 and 2 to AUDIO and connect your source.
   Leave the jack you will gate on in Gate mode, the default.
2. Set **Gate Loop** to that jack.
3. Select an empty pad on the Pads screen and open its record page. Set Rec
   Input to In 1/2, Rec Quant to 1 bar and RecToPlay to ON. Set Length to a
   number of bars, or to Custom to end each take with a gate.
4. Send a gate to record. Send another to replace the loop with a new take.

With Length set to Custom, set the recorded pad's **Sync** to **None**
(the pad's Pos page) for a clean loop. Otherwise the clip can rush towards its
end as Sync pulls it into line with the clock. See
[Clean Loops with a Custom Length](SETTINGS.md#clean-loops-with-a-custom-length).

## Building It

You need a Bitbox Micro on firmware 2.3.4, Python 3, a microSD card, and
`MICRO234.zip` from [1010music's downloads page](https://1010music.com/downloads).
Unzip it to get `MICRO.BIN`, then:

```
git clone https://github.com/ekloiber/bitbox-micro-fw-patch.git
cd bitbox-micro-fw-patch
mkdir patched
python3 patch_micro.py MICRO.BIN patched/MICRO.BIN --gate-only
```

On Windows, use `python` instead of `python3`. Copy `patched/MICRO.BIN` to the
root of the card, hold the white right-arrow button and power on. Let go when
it says Erasing.

There are two builds:

| Flag | What you get | Splash | Fingerprint begins | Tested |
|---|---|---|---|---|
| `--gate-only` | Stock 2.3.4 plus the three rows, nothing else. The rows follow SD Mode on Settings page 2. | `2.3.4-gr3` | `e187b8dc` | Yes, on a module |
| `--gate-rec` | The original project's full 2.3.10 patch plus the three rows, after Theme on page 3. | `2.3.10-gr3` | `804ba422` | Not yet |

Start with `--gate-only`. With no flag, the script builds the original
project's 2.3.10-mod unchanged; its features are documented in the guide pages
in this folder and at the
[original project](https://github.com/modularpatcher/bitbox-micro-fw-patch).

Keep your stock `MICRO.BIN` safe: flashing it is how you go back.

## How It Works, Briefly

The script edits your own copy of stock 2.3.4 and refuses any other file. It
adds the new code to the end of the image and points a few existing
instructions at it. Nothing in the original is removed. A gate on an assigned
jack sends the same internal event that MIDI Rec's action notes send, so the
firmware's own record and clear code does the work.

[Settings](SETTINGS.md#gate-rec-and-gate-clr) has the details and the
hardware test results. [Changelog](CHANGELOG.md) lists each version.

## Rules

- **Never pass a patched image on.** This repository contains no 1010music
  firmware, stock or patched, and must not. Send people here to build their
  own.
- **Assume it affects your warranty.** Flash stock firmware back before you
  send a unit for service.
- **Support:** ask in this fork's GitHub Issues, not upstream and not with
  1010music.

## Licence and Credit

Built on [modularpatcher/bitbox-micro-fw-patch](https://github.com/modularpatcher/bitbox-micro-fw-patch),
which did the groundwork: the patcher, its safety checks, and the Settings
mechanism these rows use. The patcher and this guide are MIT-licensed. See
[LICENSE](LICENSE), which also covers the Airwindows effects in the full build.
The firmware is 1010music's, and no permission over it is granted. Bitbox Micro
is a product of 1010music LLC, named only to say what hardware this runs on.

AI was used to read the firmware and write the patch. Every behaviour listed
as tested was confirmed by hand on a module.
