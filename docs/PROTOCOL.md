# Aquacomputer USB protocol notes

What AquasuiteLinux sends and receives. Most of this was worked out by the authors of the Linux
`aquacomputer_d5next` driver (mainline and the extended out-of-tree version) and liquidctl; see
[NOTICE](../NOTICE). All multi-byte values are **big-endian** unless noted, and offsets count from the
first byte of the report, which is the report ID.

## Transport

The QUADRO has two USB interfaces: interface 0 is vendor-specific with bulk endpoints 0x81 (IN) and
0x02 (OUT); interface 1 is the HID interface with one interrupt IN endpoint (0x83). AquasuiteLinux uses
the Linux **hidraw** interface for everything HID and **usbfs** for the bulk endpoint:

| Operation | call |
|---|---|
| status reports the device sends by itself | hidraw `read()` |
| read the settings report | hidraw `ioctl(HIDIOCGFEATURE)` |
| write the settings report | hidraw `ioctl(HIDIOCSFEATURE)` |
| software sensor / LEAKSHIELD feed | usbfs `USBDEVFS_BULK` to endpoint 0x02 (interface 0 claimed) |

The HID interface has no OUT endpoint, so a hidraw `write()` of the software sensor report becomes a
SET_REPORT control request, which the QUADRO rejects (EPROTO / ETIMEDOUT) — tested on real hardware.
No kernel driver uses interface 0, so claiming it doesn't disturb hidraw or the hwmon driver. The
endpoint is found through sysfs (`aquactl probe` prints the layout). The kernel driver and hidraw work
side by side. The devices need about 200 ms between settings
operations; AquasuiteLinux paces them like the kernel driver does. Feature report 0x08 (1013 bytes) holds the names
aquasuite shows for sensors and outputs: 24-byte NUL-padded Latin-1 slots after a 3-byte header, sealed with
the same CRC-16. QUADRO slots (from an aquasuite backup of a real QUADRO): fans 0–3, LED controllers 8–15,
flow 16, temperature sensors 17–20, software sensors 24–39. OCTO (from aqdctl): fans 0–7, sensors 20–23,
flow 24, software sensors 26–41. AquasuiteLinux only reads it, to label imports. aquasuite's device backup
(`<DeviceBackup>` XML) stores the settings report (padded to 1013 bytes) as item `settings` and this report
as item `flash`, both base64. The aquaero and the LEAKSHIELD expose
several HID interfaces under one product ID; the right one is chosen from the report descriptors.

## Product IDs

| Device | ID | Family |
|---|---|---|
| aquaero 5/6 | f001 | aquaero |
| high flow USB / mps flow | f003 | legacy |
| farbwerk | f00a | standard |
| aquastream ULTIMATE | f00b | standard |
| QUADRO | f00d | standard |
| D5 NEXT | f00e | standard |
| farbwerk 360 | f010 | standard |
| OCTO | f011 | standard |
| high flow NEXT | f012 | standard |
| LEAKSHIELD | f014 | standard |
| aquastream XT | f0b6 | legacy |
| poweradjust 3 | f0bd | legacy |

## Status report (ID 0x01, sent every second)

Common fields: serial number (two `u16` at 0x03 and 0x05, shown as `NNNNN-NNNNN`), firmware version
(`u16` at 0x0D), power-on count (`u32` at 0x18). Temperatures are `s16` hundredths of a degree;
`0x7FFF` means not connected. Flow is in dL/h (AquasuiteLinux shows L/h).

Fan / pump substructure (Quadro, Octo, D5 NEXT):

| Offset | Value |
|---|---|
| +0x00 | output power, hundredths of a percent |
| +0x02 | voltage, hundredths of a volt |
| +0x04 | current, mA |
| +0x06 | power, hundredths of a watt |
| +0x08 | speed, rpm |

QUADRO: temperatures 0x34–0x3A, software sensors 0x3C–0x5A, software sensor types 0x5C (16 bytes), supply
voltage 0x6C, flow 0x6E, fans 0x70 / 0x7D / 0x8A / 0x97. OCTO: temperatures 0x3D, software sensors 0x45,
flow 0x7B, fans 0x7D + 13·n. D5 NEXT: coolant 0x57, flow 0x59, software sensors 0x3F, +12 V 0x37, +5 V
0x39, pump 0x6C, fan 0x5F. The complete table for every device is in `aquasuitelinux/core/devices.py`.

Legacy devices (aquastream XT, poweradjust 3, high flow USB) don't send reports; their status is read with
GET_FEATURE (IDs 0x04, 0x03 and 0x02) and uses little-endian values.

## Settings report (ID 0x03; aquaero 0x0B)

Read with GET_FEATURE, modified, and written back with SET_FEATURE, followed by the "save" report
`02 00 00 00 02 00 00 00 00 34 C6` (aquaero: `06 00 02 00 00 00 00`). AquasuiteLinux sends the save report
as a feature report, like the kernel driver; USB captures of aquasuite (in the aqdctl project) show
aquasuite sending it as an *output* report (SET_REPORT with report type 2). The last two bytes are a
**CRC-16/USB** (poly 0x8005 reflected, init 0xFFFF, xorout 0xFFFF) over bytes 1 … n−3. The aquaero and
aquastream XT use no checksum. Lengths: QUADRO 0x3C1, OCTO 0x65F, D5 NEXT 0x329, farbwerk 360 0x682,
aquaero 0xA93.

| QUADRO offset | Value |
|---|---|
| 0x06 | flow sensor pulses per litre |
| 0x0A | temperature offsets, 4 × `s16` hundredths of a kelvin |
| 0x12 + 9·n | setup of fan n: flags, min power, max power, fallback power (see below) |
| 0x36 / 0x8B / 0xE0 / 0x135 | control of fans 1–4 (see below) |
| 0x3BD | active profile |

Setup record: `u8` flags (bit 1 hold minimum power, bit 2 start boost), then `u16` minimum, maximum and
fallback power in hundredths of a percent. OCTO: 0x12 + 9·n; D5 NEXT: fan flags 0x2F / min 0x30, pump min
0x39 (no flags).

Control substructure of one fan:

| Offset | Value |
|---|---|
| +0x00 | mode: 0 manual, 1 PID (target temperature), 2 curve, 3 + n follow fan n + 1 |
| +0x01 | manual power, hundredths of a percent |
| +0x03 | controller input: 0–3 physical sensors, 4 + k software sensor k + 1, 0xFFFF none |
| +0x05 | PID: target temperature, P, I, D1, D2, hysteresis |
| +0x13 | curve start value |
| +0x15 | curve: 16 temperatures, hundredths of a degree |
| +0x35 | curve: 16 powers, hundredths of a percent |

OCTO fan controls: 0x5A + 0x55·n. D5 NEXT: pump 0x96, fan 0x41.

## Software sensor report (output report, ID 0x04)

Feeds values into the device's software sensors (what aquasuite uses for a Delta T or the CPU temperature
on the device). 67 bytes:

| Offset | Value |
|---|---|
| 0x01 | 16 × `s16` values, hundredths; `0x7FFF` = unavailable |
| 0x21 | 16 × type: 0 disabled, 3 temperature, 5 percent, 7 power |
| 0x31 | 16 × `0x64` |
| 0x41 | CRC-16/USB over bytes 1 … 0x40 |

AquasuiteLinux builds byte-for-byte the report captured from aquasuite and sends it every second as a
bulk transfer to endpoint 0x02 — only when *Send sensor values to devices* is on, because the bulk path
rests on one remark in the kernel driver's reverse-engineering notes ("this seems to be a usb bulk
transfer") and isn't confirmed on hardware yet. `aquactl doctor --feed-test` checks it on a device. Before storing a curve that reads a software sensor on the device, it
checks that the device reports the values back in its status report; if the transfer fails or the values
don't come back, those outputs are controlled in software instead.

## LEAKSHIELD feed (output report, ID 0x04, 51 bytes)

Pump speed (`u16` at 1, unit byte 0x03 at 33) and flow (`u16` dL/h at 3, unit byte 0x0C at 34); the other
slots are `0x7FFF`. The CRC-16/USB covers bytes 0 … 48 and is stored at 49.

## aquaero control

A fan is switched to manual power by pointing its control source (fan control + 0x10) at its power preset
(0x5C + n), writing the power to the preset (0x55C + 2n) and setting minimum power 0 and maximum power
100 % (fan control + 0x04 / + 0x06). Fan controls start at 0x20C, 0x220, 0x234, 0x248.

## aquastream XT control (ID 0x06, 0x34 bytes, little-endian, no checksum)

Pump: speed as `45000000 / rpm` at 0x08 (3000–6000 rpm), manual mode 0x14 at 0x03. Fan: 0–255 at 0x1B,
manual mode 0x01 at 0x1A. Save report `02 05 00 00`.
