# Bitbox Micro, Unlocked

The Bitbox Micro hides most of its controls behind the screen. This patch puts
them on MIDI and adds what the module never had:

- Airwindows effects across the whole mix: four slots, sixteen effects.
- A master EQ and filter, and full control of the compressor.
- Per-pad sends, envelope, LFO, loop and granular controls.
- A pop-up showing what every CC just changed.

It all works live, while you play.

It is unofficial. Nobody at 1010music wrote or endorsed it, so do not ask them
for support. The splash reads `community fw`, so a patched unit is never
mistaken for theirs. Assume it affects your warranty. It
[cannot brick the module](INSTALLING.md#can-this-brick-my-module), though.

Help documentation: https://modularpatcher.github.io/bitbox-micro-fw-patch/

## A Patcher, Not Firmware

You download a script, not firmware. It edits your own copy of stock 2.3.4, on
your own machine, and refuses anything else. Nobody here has 1010music's code
to distribute.

There is one rule: **never pass a patched image on**. Send people this page
instead, and let them build their own.

## Quick Start

You need a Bitbox Micro on firmware 2.3.4, Python 3 and a microSD card. Get
[`patch_micro.py`](patch_micro.py), and `MICRO234.zip` from 1010music. Run the
script on the zip and copy the result to your card. The splash should then read
`community fw`. [Installing](INSTALLING.md) has every step.

## Read Next

- [Installing](INSTALLING.md): from stock image to patched module, step by step.
- [CC reference](CC-REFERENCE.md): every number, its channel, and what to check
  when one seems dead.
- [Pad controls](PAD-CONTROLS.md): what each pad parameter does.
- [Devices](DEVICES.md): the three that act on the whole mix, and where they sit.
- [The Character chain](CHARACTER.md): sixteen Airwindows effects in four slots.
- [The EQ and filter](EQ-FILTER.md): ten algorithms on one encoder.
- [The compressor](COMPRESSOR.md): bus glue to start, then every setting on
  MIDI.
- [Settings](SETTINGS.md): the pop-up, Preset BPM, Keep CCs and more.
- [Limitations](LIMITATIONS.md): what the patch will not do, and why.
- [Changelog](CHANGELOG.md): what changed, newest first.

## Licence

The patcher and this guide are MIT-licensed. The firmware is 1010music's. No
permission over it is granted. Bitbox Micro is a product of 1010music LLC, named
only to say what hardware this runs on. The Character chain's effects are ports
of Chris Johnson's Airwindows plugins, under their own MIT licence. If you paid
for this, somebody sold you something they did not write.

Flash stock firmware back before you send a unit for service. See
[LICENSE](LICENSE) for the rest.

## AI Assistance

Yes, AI was used. It helped read the assembly for the patch and draft this
guide, which left more hours for designing controls and testing them.

It could not decide whether any of it works. Every control was confirmed by ear,
on hardware. Judge the patch by what it does for you.

## If You're from 1010music

Thank you for the Bitbox Micro. It is a wonderful little module, and worth the
time spent inside it. No firmware of yours is included, in any form. Everybody
downloads it from your site, under your terms, and the script refuses anything
else. What ships is one Python file, a guide, and an MIT licence covering only
those. The splash reads `community fw`, and anyone with a problem is told to
flash your firmware back before contacting you.

## Getting Help

There is no support. It is one person's patch, given away as is. Ask in
[the Mod Wiggler thread](https://www.modwiggler.com/forum/viewtopic.php?t=302078),
r/modular or r/eurorack, where other users run the same controls on the same
numbers. Please do not take questions to 1010music's Discord. It is not their
work.
