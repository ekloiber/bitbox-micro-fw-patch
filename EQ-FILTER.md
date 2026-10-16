# Bitbox Micro Firmware: The Master EQ and Filter

The patch adds a master EQ and filter across the whole mix, which the module
never had. Ten modes sit on one encoder: a DJ sweep, a dual cut and a band pass,
then shelving and bell EQs, a tilt, a notch, a phaser and a formant filter. One
knob picks the mode. Three more play it.

It comes after the Character chain and before the compressor, so it shapes the
delay and reverb tails too. Out 3–6 stay clean. See
[where they sit](DEVICES.md#where-they-sit).

## Centre Is Always Neutral

In every mode, every control is neutral at its centre. With all three knobs
centred, the EQ does nothing.

You can sweep the mode dial with the controls centred and hear nothing, then
pick a mode and move one knob. To back out of anything, return that knob to
centre.

On a controller with endless encoders, give each of the three a centre-line LED
style and a reset-to-middle gesture. You will use both constantly.

## The Ten Modes

| # | Mode | Encoder 2 | Encoder 3 | Encoder 4 |
| - | ---- | --------- | --------- | --------- |
| **1** | **DJ Filter** | filter, dull to thin | resonance | knee |
| **2** | **Dual Cut** | high cut | low cut | resonance |
| **3** | **Band Pass** | centre frequency | width | gain |
| **4** | **Mixer EQ** | high | mid | low |
| **5** | **Tone + Filter** | high | low | filter, dull to thin |
| **6** | **Tilt** | tilt | hinge frequency | — |
| **7** | **Notch** | frequency | Q | depth |
| **8** | **Peak** | frequency | Q | gain |
| **9** | **Phaser** | sweep | depth | spread |
| **10** | **Formant** | vowel, I to U | intensity | shift |

- **DJ Filter**: the one-knob sweep. Anticlockwise closes it to a dull
  thump, and clockwise thins it to hats.
- **Dual Cut**: a cut at each end, set separately.
- **Band Pass**: a band you can move, narrow and lift.
- **Mixer EQ**: a three-band desk EQ.
- **Tone + Filter**: two shelves and a filter on one panel.
- **Tilt**: a see-saw, lifting the treble by exactly what it drops the bass.
- **Notch** and **Peak**: one surgical cut and one sweepable bell.
- **Phaser**: four notches sweeping together as one body.
- **Formant**: three bells on the resonances of a human vocal tract, morphing
  between vowels.

## Working the Four Encoders

The EQ uses nine CCs, all on **MIDI channel 1**.

| Control | Turn | Push |
| ------- | ---- | ---- |
| **Encoder 1** | CC **50** — mode | CC **57** — master bypass |
| **Encoder 2** | CC **51** | CC **54** |
| **Encoder 3** | CC **52** | CC **55** |
| **Encoder 4** | CC **53** | CC **56** |
| **A button** | — | CC **59** — filter slope, 12 or 24 dB |

Encoders 2, 3 and 4 should be plain linear encoders across the full range. Set
the mode encoder to **ten steps from 6 to 122**, not the full sweep. Each click
then lands in the middle of a mode, never on a boundary.

Send CC 50 as plain 7-bit. As a 14-bit pair it would also move a pad's granular
window every time you changed mode.

The pop-up names the mode, and each control by that mode's name for it, with its
value in hertz, decibels, octaves or a vowel. Pushes show too, such as
`Kill High  On`.

## Kill, Open and Slam

The pushes are momentary performance controls. Hold a push to act. Let go to
return. The encoder's own position comes back untouched, so nothing jumps.

| Gesture | What it does | Where |
| ------- | ------------ | ----- |
| **Kill** | that range disappears from the mix | Mixer EQ's three, Tone + Filter's two |
| **Open** | that band stops acting, letting more through | Dual Cut, Band Pass width, Tone + Filter's filter |
| **Slam** | forces a control to maximum effect | Notch, Tilt, Peak, Phaser, Formant |

Sixteen pushes do something across the ten modes. On the rest, spend the push
on a reset-to-centre gesture instead. Most controllers switch a push between
the two with one setting.

## Bypass and Slope

CC 57 takes the whole EQ out. Press once and it is gone. Press again and it is
back. Unlike the other pushes, it latches. Give it encoder 1's push.

CC 59 switches the filter slope between 12 and 24 dB per octave, on the modes
that have one. Set it up as an on-off button. Band Pass ignores it, because its
gain control needs the same bands and gain wins.

## How the EQ Arrives

It powers on in **DJ Filter**, with all three controls centred, bypass off and
the slope at 12 dB. Centred controls pass everything through, so a freshly
flashed module sounds exactly as before. Like the other devices, its settings
survive a preset change and reset at power-on. See
[what they have in common](DEVICES.md#what-they-have-in-common).
