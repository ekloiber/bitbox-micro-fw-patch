# Bitbox Micro Firmware: Settings

Three of the patch's additions are not on a CC. They sit on the module's own
Settings screen, beside 1010music's, and the module saves them with the rest of
its settings. Set them once. They stay set, through preset changes and power
cycles.

Press TOOLS once to open Settings. Turn the top knob to pick a row and the
bottom knob to change it. All three are on the second page, in this order:

```text
ProgChange:  OFF / ON / NEW ONLY
Clk Loss:    INTERNAL / HOLD
SD Mode:     1010music's, unchanged
Flip UI:     off or on
```

## Clk Loss

Keeps the tempo when the clock goes away.

### What It Fixes

With an external clock connected, the module runs at the clock's tempo. Unplug
it, or let a clock simply stop sending, and after about five seconds the module
gives up on it. It falls back to the preset's own tempo, which is 120 BPM in
nearly every preset. Every beat-synced delay and LFO jumps with it.

HOLD stops the jump. The module carries on at the last tempo the clock gave it,
as if the clock were still there.

- INTERNAL: what the module has always done. The default.
- HOLD: keep the last external tempo until a clock returns.

The fallback tempo lives in each preset's `preset.xml`, as `globtempo`. That is
where the 120 comes from. The module has no control for it, so edit the file if
you want a different one.

### What HOLD Keeps Through

A held tempo survives loading another preset, by Program Change or from the
panel. It survives Stop and Start too.

A returning clock always takes over, at whatever tempo it now runs. So does a
second clock source that is still running. Holding only ever fills a gap.

Power off and the held tempo is gone. The setting is kept. The module starts at
the preset's tempo, as always, until a clock arrives.

One delay to know about. Switching to INTERNAL while holding takes effect at the
next Start, locate or preset load, not at once.

## Flip UI

For a module mounted upside down. Flip UI turns the screen, touch, buttons and
knobs the other way up, all together.

### What Turns

- Screen: rotated half a turn.
- Touch: follows the screen, so a tap lands on what is under your finger.
- Buttons: swap ends, so each sits where it should.
- Knobs: swap jobs. Turning direction is unchanged, because clockwise is still
  clockwise to your hand.

The knob swap happens the moment you change the setting. The knob you just used
starts doing the other job. Expect it.

### What Stays Put

The printing on the faceplate stays the right way up for a normal mount. So
does the screen the bootloader shows while flashing.

At power-on the splash and the first screen appear the normal way up for about
a second, then turn. The setting lives on the SD card, and the module draws its
first screen before it has read the card. Nothing is wrong.

## ProgChange

Stock firmware offers OFF and ON. The patch adds a third choice.

### NEW ONLY

- OFF: ignore Program Change.
- ON: always load, including reloading the preset already playing. Unchanged.
- NEW ONLY: skip a request for the preset already loaded.

Some sequencers send the same Program Change every time a pattern starts. On ON,
every repeat reloads the preset, which interrupts it and swallows the first
notes. NEW ONLY lets the repeats go by. A different preset still loads.

Going from A to B and back to A works. So does loading B from the panel and then
sending A. After you rename the current preset or use Save As, the first request
reloads it once, and repeats after that are skipped. Choose ON when you want a
resend to reload a preset you have just edited.

Program Change still answers on the MIDI Pads and MIDI Keys channels, as before.
