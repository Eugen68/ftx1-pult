**English / Русский:** Choose the interface language in the remote control settings or the server panel. See [language settings and translation instructions](LANGUAGES.md).

# FTX-1 Pult — remote control for the Yaesu FTX-1

A full remote control for the FTX-1 (Field and Optima), using the transceiver's USB cable:
CAT control, receive audio through PC/phone speakers, transmission from a PC/phone microphone,
a waterfall, all controls, and the transceiver's complete EX menu.

The FTX-1 connects by USB to a Windows PC running the server (CAT and USB audio).
Open `http://localhost:8080` on that PC, or connect a browser on another Windows PC
or Android phone over Wi-Fi, LAN, or VPN. On Android, the remote control can be installed as an app.

## Features

* VFO-A/B tuning: a circular tuning knob (rotate around its center, 100 divisions per revolution, with inertia),
  frequency digits (mouse wheel or click a digit), ± buttons, arrow keys, and direct entry (`7.074`, `14200`, `145500000`).
* Bands from 160 m to 70 cm, remembering the last frequency and mode; all operating modes.
* Meters: S-meter on receive; PO, SWR, and ALC on transmit.
* Waterfall and spectrum with an Auto span (based on the transceiver's actual audio bandwidth: approximately 3.5 kHz in SSB and 9 kHz in FM)
  or a 3–24 kHz span; filter passband, IF SHIFT, notch, and CW tone. Double-click or double-tap to tune to a signal.
* Remote squelch (FM only): follows the transceiver's squelch (SQL, RI command) or a level threshold;
  post-transmission tail muting works in all modes.
* On desktop monitors, the remote control and server panel fit the window without page scrolling (automatic or manual scaling);
  on phones, the interface becomes a scrollable column.
* Receive controls: AF, RF, SQL, AGC, IPO/AMP1/AMP2, ATT, NB, DNR, DNF, manual notch, CONTOUR, APF,
  WIDTH, IF SHIFT, NAR, CLAR (RIT/XIT), and dual receive.
* Transmit controls: power, MIC GAIN, PROC, AMC, VOX, monitor, CTCSS/DCS, repeater shift, TXW,
  and tuner (internal SPA-1, external FC-40, ATAS).
* VFO/memory: A→B, B→A, A⇄B, SPLIT, MAIN/SUB, scanning, memory channels, QMB, LOCK, and favorite frequencies.
* CW: speed, pitch, keyer, BREAK-IN, and five keyer memories (written to and transmitted by the transceiver itself).
* **FT8 and FT4** with automatic QSO sequencing and log forwarding to HAMLOG Agent (UDP, as in WSJT-X).
* CW decoder with an AI assistant, RTTY receive/transmit, weather fax, NAVTEX, SSTV, and PSK31 — see below.
* **Complete EX menu** — 402 searchable items, with read and write support.
* CAT console for arbitrary commands.
* Protection: transmission is stopped if the client connection is lost for more than 1.5 seconds or
  `max_tx_seconds` is exceeded. Only one client can transmit at a time.
* English and Russian interfaces, including the server panel and the browser interface opened from the phone's QR code.

## Choosing the language

In the **server panel**, find **Startup** (shown as **Запуск** in Russian) and select **English** in the language selector.
You can also change the language at the top of the remote control's **Settings** tab (**Настройки** in Russian).

The selection is saved in `config.json` and applies to the server panel and all connected remote controls,
including phones. Changing the language reconnects the interfaces. Stop transmission, automatic transmission,
and memory operations before switching languages. The default language is Russian (`ru`); English uses `en`.
See [LANGUAGES.md](LANGUAGES.md) for details.

## CW · Morse: decoder and AI assistant

Open the **CW · Morse** tab in the remote control.

* The **decoder** decodes Morse from receive audio in real time. It finds the tone automatically
  (or right-click a signal on the waterfall), detects speeds from 5 to 45 WPM, and supports Latin or Russian Morse alphabets.
  Decoding uses built-in digital signal processing, as in fldigi.
* The **AI assistant** (LM Studio running on this PC) corrects decoder errors after each transmission,
  translates and explains abbreviations and Q-codes in the selected interface language, extracts the callsign, RST,
  name, and QTH, and flags calls directed to you. **✨ AI reply** drafts a reply based on the QSO so far.
* **Text transmission**: use the text field with Ctrl+Enter, or the CQ / Reply / Report / 73 / AGN? / QRZ? macros.
  By default, the transceiver sends the text from its keyer memory (50-character chunks, memory 5).
  Alternatively, key the DTR/RTS line on the second port (transceiver menu `CW_PC_KEYING`).
* **AI auto-reply**: when the other station calls your callsign and finishes transmitting, the AI prepares
  a reply and transmits it after a few seconds. You can cancel it; Esc stops transmission. It replies only
  when your callsign is actually present in the received text, and limits the number of consecutive replies.

**LM Studio setup**: load a model. For a GPU with 12 GB of VRAM, examples include Qwen2.5 7B/14B
Instruct, Gemma 3 12B, and Mistral Nemo 12B. Enable the server using Developer → Start Server.
Set the address and model in the server panel's **AI and CW** card.

The language model does not listen to audio: it works with text produced by the decoder.

**Everything also works without LM Studio**: decoding, transmission, macros, and automatic replies.
Received text (callsign, RST, name, QTH, and abbreviations) is then interpreted using CW QSO rules,
and replies follow a standard QSO template (reply to CQ → report with name and QTH → 73 SK).
LM Studio improves error correction and produces more flexible replies. If its server is unavailable,
the remote control reports this once and continues using the rule-based fallback.

## QSO recording

The **⏺ Record** button at the top of the remote control (H key) records receive and transmit audio
into one WAV file (16 kHz) in the `records` folder next to the program. Recent recordings can be downloaded
from the **Settings** tab, including to a phone.

## Voice auto-call

Open **Transmit** → **Voice auto-call**. Store up to five messages by recording from this device's microphone
or uploading an audio file. Silence at the beginning and end is trimmed and volume is normalized.
Start transmission to repeat a message: transmit, pause to receive, then transmit again, for the selected number of cycles.
Stop with PTT, Esc, the **Stop** button, or a signal during the receive pause at or above the selected level (S3…S9).
Messages are stored on the server in the `voice` folder next to the program.

## NAVTEX, SSTV, PSK31

The **Digital** tab contains RTTY, Weather fax, NAVTEX, SSTV, and PSK31 sections.

* **NAVTEX** — maritime warnings and forecasts on 518 kHz (also 490 and 4209.5 kHz).
  Includes Black Sea stations (Novorossiysk A, Kerch B, Odesa C, Istanbul D, Samsun E, Varna J, etc.)
  with schedules. Set the transceiver to USB, 1.7 kHz below the listed frequency. ZCZC headers are parsed
  to identify the station and message category. **Translate (AI)** translates the message and requires LM Studio.
* **SSTV** — images in Martin M1/M2, Scottie S1/S2, Robot 36, and PD 90/120/180 modes.
  The mode is detected from its VIS code, or from synchronization pulses if the beginning was missed.
  Slant and tuning errors are corrected automatically. Images are saved in the `sstv` folder.
* **PSK31** — receive and transmit in DATA-U. Right-click a trace on the waterfall to tune;
  automatic frequency control then tracks it. CQ / DE / 73 macros are available.

## Memory channel editor

Open **VFO/Memory** → **Channel editor…** for a full-screen table of all channels, similar to ADMS / RT Systems:
number, group, name (12 characters), frequency, mode, shift (simplex/+/−), offset, transmit frequency,
tone mode (off / ENC / TSQ / DCS), CTCSS, DCS, clarifier, and note.

* **Read** — channels 001–999, or a selected range. The tone option also reads the CTCSS number/DCS code;
  the transceiver briefly switches to each channel with a tone, then returns.
* Changes are highlighted. **Write changed** writes only modified channels and verifies them by reading back (✔ / ⚠ beside each row).
* Custom groups appear on the left: assign selected channels, filter, or rename. Other tools include **← from VFO**,
  moving channels to other numbers, sorting by frequency, undoing edits, and searching.
* Channel sets are saved on the PC in the `memory` folder next to the program. Import CSV files
  in the application's format or CHIRP format, and export CSV.
* Channel writing, checked on an FTX-1 with firmware 1.13: frequency, mode, shift, and clarifier use MW;
  CTCSS and DCS numbers use VFO-A and AM (VFO-A to memory), after which VFO-A and the transceiver's previous state are restored.
  Names use MT; a separate transmit frequency uses MZ. Channels cannot be cleared over CAT.
  The repeater offset amount is shared within each band (FM → RPT SHIFT menu). Groups are not stored in the transceiver.

## FT8 and FT4 — an MSHV-style station in your browser

The **FT8** tab provides receive, transmit, and automatic QSO sequencing from a PC or phone.

* Enable reception and select a band: the transceiver switches to DATA-U on the standard FT8/FT4 frequency.
  Decoding runs at the end of each period (15 seconds for FT8, 7.5 seconds for FT4). CQ calls appear in green; calls to you appear in red.
* **Click a decoded line** to call that station and run the QSO automatically: grid → report → R-report → RR73/73.
  **CQ** sends a general call; the first responding station is handled automatically. **Auto-CQ** resumes calling after each QSO.
* Set the transmit frequency by right-clicking the waterfall or using the **TX, Hz** field. **Stop** immediately stops transmission.
* The PC clock must be accurate to ±0.5 seconds. The average dt offset of received stations appears beside the clock.
* **Logging**: each QSO is written to `ft8_log.adi` next to the program and sent over UDP in WSJT-X format
  (default 127.0.0.1:2237), supported by HAMLOG Agent, Log4OM, JTAlert, and GridTracker.
  Enable WSJT-X input on this port in your logging software. Close JTDX/MSHV so the serial port and audio device are available.
* Configure your callsign, grid, power, and logging destination in **FT8 settings** on the same tab.

**New countries and callsigns** (similar to JTAlert): decoded lines are marked by color.
Purple means a country not yet worked; orange means a country not worked on this band; blue means a new callsign.
Faded lines indicate stations already worked on this band. The new-and-to-me filter keeps only relevant lines.
A new country triggers a notification, optionally with sound. Automatic CQ prioritizes new countries.
To include your entire log, import an ADIF export (for example, from HAMLOG) in **FT8 settings**;
QSOs made in this application are added automatically. Countries are identified using `cty.dat`
(Country Files, AD1C, www.country-files.com), which the program updates monthly.

**PSK Reporter**: received stations are reported to pskreporter.info approximately every five minutes,
as in WSJT-X/JTDX. The reception reports section shows stations that heard your transmissions during
the last 30 minutes, including distances. **Map** opens the map on the website.

The FT8/FT4 decoder and encoder use [ft8_lib](https://github.com/kgoba/ft8_lib)
by Karlis Goba (MIT license), located in `server/ftx/lib`. QSO sequencing, transmission control, and logging are implemented by this project.

## RTTY and weather fax

Open the **RTTY · Fax** tab.

* **RTTY**: reception at 45.45 / 50 / 75 / 100 baud, with 170–850 Hz shift. Polarity (LSB/USB, reverse)
  is detected automatically. Auto-tune finds a tone pair in the passband; right-clicking the waterfall sets
  the decoder center. Presets include amateur RTTY and DWD weather reports (50 baud / 450 Hz).
  Transmission uses USB audio (AFSK) in **DATA-L/DATA-U**, with CQ / RYRY / 73 macros.
  **Translate (AI)** translates and explains a report using LM Studio; reception works without it.
* **Weather fax (WEFAX)**: 120 lines/minute, IOC 576 (also 60/90/240 and IOC 288).
  Reception starts automatically on the start tone; phasing aligns the map edge, and slant is corrected automatically.
  The stop tone saves the map as a PNG in the `fax` folder next to the program. Click the image to set its left edge manually;
  ◀ ▶ adjust slant, ⛶ opens full screen, and 💾 downloads the PNG.
  DWD stations (Pinneberg): 3855, 7880, and 13882.5 kHz. Set the transceiver to DATA-U, 1.9 kHz below the listed frequency.

Station frequencies and schedules change; check current listings.

## Keyboard shortcuts

Shortcuts work with either Russian or English keyboard layouts. Press **?** or **F1**, or click ⌨ at the top,
for help. Reassign any shortcut under **Settings** → **Keyboard shortcuts**.

| Key | Action |
|---|---|
| **Space** (hold) | PTT — transmit |
| Esc | Stop transmitting; stop the tuner |
| ↑ → / ↓ ← | Tune one step up / down |
| PgUp / PgDn, Shift + arrows | Tune 10 steps |
| S / Shift + S | Larger / smaller tuning step |
| Digits, Enter | Enter a frequency (7.074 = MHz, 14200 = kHz) |
| ] / [ | Next / previous band |
| U, L, C, A, F, D | USB, LSB, CW, AM, FM, DATA (press again for CW-U⇄CW-L, FM⇄FM-N, DATA-U⇄DATA-L) |
| M / Shift + M | Next / previous operating mode |
| V, B, X | A⇄B, A→B, SPLIT |
| K | LOCK |
| T | Tuner |
| N, R, J, O, P, G | NB, DNR, DNF, ATT, preamplifier, AGC |
| Q | Remote squelch (FM) |
| Z | Receive audio on/off |
| = / − | Volume |
| W | Waterfall span |
| Shift + 1…5 | Transmit CW memory 1…5 |
| E | Morse decoder on/off |
| I / Y | AI: analyze received text / draft a reply |
| Backtick (Ё on a Russian keyboard) | Focus the CW text field; Ctrl+Enter transmits |

## Understanding the waterfall

The FTX-1 **does not send its scope image over CAT**. The `SS` command only changes scope settings
on the transceiver's screen. The remote waterfall is therefore generated from USB codec audio:
it shows the receiver passband (up to 24 kHz depending on the filter; approximately 3–4 kHz is useful in SSB),
rather than the wide band view shown on the transceiver. This is sufficient for FT8/CW/SSB.
A wide band display requires an additional SDR receiver.

## Installation (Windows)

First install Yaesu's **Virtual COM Port (CP210x)** driver and connect the FTX-1 by USB.
Windows Device Manager should show an *Enhanced COM Port*, a *Standard COM Port*, and a *USB Audio CODEC* device.

**Option A — prebuilt FTX1-Pult.exe** (no Python required): download the executable and double-click it.
For the English/Russian version in this fork, use the Windows build from
[GitHub Actions](https://github.com/Eugen68/ftx1-pult/actions/workflows/build.yml)
on the `feat/english-language` branch. The original project's
[latest release](https://github.com/Evgeny161rus/ftx1-pult/releases/tag/latest)
is a separate upstream build and may not include this fork's language changes.
The workflow rebuilds the executable on pushes to `main` and `feat/english-language`.
To build locally, run `build_exe.bat`.

**Option B — run from source.** Install Python 3.10+ from python.org, selecting *Add python.exe to PATH*,
then double-click `FTX1-Pult.pyw`. On first launch, the program offers to install the required libraries.

### Server control panel

Starting the program opens the server window without a console and adds a system tray icon.

* **Transceiver** — COM port, baud rate, separate receive/transmit audio devices, and PTT method.
  **Find** detects the FTX-1 automatically: Enhanced port for CAT, Standard port for PTT, and USB Audio CODEC for audio.
* **▶ Start / ■ Stop** — connect to or disconnect from the transceiver. The indicator shows connected / not responding / TRANSMITTING,
  with frequency and mode beside it.
* **Open remote** — open the remote control on this PC.
* **Phone and other PCs** — password, addresses, and a **QR code** to scan with the phone's camera.
* **Startup** — interface language, start with Windows (in the tray without a window), connect to the transceiver automatically,
  open the remote control on startup, and maximum transmission duration.
* **Connected** — current remote users and the transmitting client; **Log** shows events.
* **Exit application** — stop the server completely. Closing the window with **X** leaves the server running
  in the tray; launching the program again opens that same server's panel.

Settings are saved automatically; you do not need to edit `config.json` manually.
The simulator checkbox in the **Transceiver** card lets you try the application without a radio.

### Closing the application

Use **Exit application** in the server panel or the tray icon menu to shut down completely.
Pending panel settings are saved, transmission is stopped, background tasks are cancelled,
and the server's serial ports, audio devices, and network listeners are closed.
The window's **X** button only hides the panel in the tray. Closing a browser tab or the phone interface
also leaves the server running. These behaviors are intentional.

### Transceiver setup for a PC microphone

In the transceiver menu, accessible through the remote control's **Menu** tab → **Show these items**:

| Item | Purpose |
|---|---|
| `SSB_MOD_SOURCE`, `AM_MOD_SOURCE`, `FM_MOD_SOURCE`, `DATA_…` | Modulation source: select USB (check the FTX-1 manual for the value number) |
| `…_USB_MOD_GAIN` | Audio level from the PC to the transmitter |
| `…_USB_OUT_LEVEL` | Receive audio level sent to the PC |

Adjust levels using the ALC meter: normal speech should move ALC into the beginning of its range, without overdriving.
The CAT baud rate in the transceiver menu must match the panel setting (default 38400).

## Android phones and other PCs

1. Connect the phone to the same Wi-Fi network as the PC connected to the transceiver.
2. Scan the QR code in the server panel, or open an address such as `https://192.168.x.x:8443` in Chrome.
3. The certificate is self-signed: choose **Advanced** → **Proceed to the site**. HTTPS is required
   because the browser will not allow microphone access without it.
4. Enter the password set in the server panel.
5. In Chrome's menu, choose **Install app** to add an FTX-1 icon like a regular application.

The phone uses the same remote interface and saved language setting as the PC.
Select **English** in the server panel or the remote control's **Settings** tab.

For access over the internet, use a VPN (WireGuard, ZeroTier, Tailscale, etc.) and the PC's VPN address,
rather than exposing the server port through the router. For mobile data, select 8 kHz audio quality
in the remote control settings. On the PC connected to the transceiver, the remote control defaults
to 48 kHz audio without resampling.

## config.json (advanced; these options are available in the panel)

| Key | Default | Description |
|---|---|---|
| `language` | `"ru"` | Interface language: `ru` for Russian or `en` for English; shared by the panel and all remote controls |
| `cat_port` | `""` | FTX-1 Enhanced COM port |
| `cat_baud` | `38400` | CAT baud rate, matching the transceiver menu |
| `ptt_method` | `TX1` | `TX1`: CAT PTT command; `TX2`: alternative DATA command; `RTS`: RTS/DTR line |
| `ptt_port`, `ptt_line` | `""`, `RTS` | For `ptt_method: RTS`: Standard COM port and RTS/DTR line; see transceiver menu `…_RPTT_SELECT` |
| `audio_in_name`, `audio_out_name` | `""` | Transceiver receive input and transmit output; empty selects automatically (USB Audio CODEC / USB Audio Device) |
| `audio_in_index`, `audio_out_index` | `null` | Device indices, normally unnecessary because devices are selected in the panel |
| `http_port` | `8080` | Remote control on the local PC (`http://localhost:8080`) |
| `https_port` | `8443` | Remote control for phones and other PCs |
| `password` | `""` | Password for access from other devices; empty disables remote access |
| `max_tx_seconds` | `180` | Maximum duration of a single transmission |
| `auto_connect` | `true` | Connect to the transceiver on startup |
| `open_panel`, `open_pult` | `true`, `false` | Open the server panel / remote control on startup |
| `simulate` | `false` | Use the simulator instead of a transceiver |
| `audio_api` | `""` | Preferred audio API (MME/WASAPI) |

## Troubleshooting

* **Port cannot be opened** — another program (WSJT-X, a logger) may be using it.
  Close that program or configure it to connect through flrig/rigctld. Also check that the selected COM port exists.
* **Transceiver does not respond** — check that the Enhanced COM port is selected and that the CAT baud rates match.
* Error details appear in the panel's log and in `ftx1-pult.log` next to the program.
* **No receive audio / empty waterfall** — select the transceiver's **Microphone (… USB Audio …)** input
  and **Speakers (… USB Audio …)** output in the server panel. Windows may call the device *USB Audio CODEC*
  or *USB Audio Device*. Enable audio in the remote control and check `…_USB_OUT_LEVEL`.
  A dark waterfall is normal in FM when the squelch is closed.
* **Transmission without modulation** — `…_MOD_SOURCE` is not set to USB for the current mode,
  or `…_USB_MOD_GAIN` is too low.
* **PTT does not transmit** — try `"ptt_method": "TX2"` or RTS through the Standard port.
* **Echo or feedback on the phone** — enable receive muting during transmission and echo cancellation; use a headset.
* **Cannot find the language selector** — fully exit any older running copy using its tray menu,
  then launch the updated executable. A new launch can otherwise reopen the panel of the already running server.
* **Closing the window does not stop the server** — use **Exit application** in the panel or tray menu; **X** only hides the window.

## Project layout

* `server/cat.py` — FTX-1 CAT protocol: command queue, polling, response parsing, and Auto Information.
  Command formats were checked against CAT Operation Reference Manual 2508-C, using Hamlib's FTX-1 driver as a reference.
* `server/audio.py` — 48 kHz USB audio, 16/8 kHz resampling, waterfall FFT, and transmit jitter buffer.
* `server/webserver.py` — HTTP/HTTPS and WebSocket server without external libraries.
* `server/sim.py` — transceiver and on-air simulator, including a UA3ABC Morse operator that conducts a QSO.
* `server/cw.py` — CW decoding and transmission; `server/ai.py` — LM Studio and rule-based fallback;
  `server/cwdesk.py` — QSO sequencing and automatic replies.
* `server/ft8.py` — FT8/FT4 time slots, QSO sequencing, transmission, ADIF logging, and UDP logging;
  `server/ftx/` — the `_ftx` extension based on ft8_lib (MIT). Build with `python server/setup_ftx.py build_ext --inplace`.
* `server/digi.py` — RTTY and weather fax; `server/navtex.py`, `server/sstv.py`, `server/psk31.py` — NAVTEX, SSTV, PSK31;
  `server/digidesk.py` — server-side integration for these modes.
* `server/main.py` — server, control panel, startup integration, and tray icon.
* `server/background.py` — cancellable background network work used during server operation and shutdown.
* `server/i18n.py`, `web/i18n.js`, `web/locales/en.json` — shared English/Russian interface translation.
* `FTX1-Pult.pyw` — double-click launcher without a console.
* `server/menu_ftx1.json` — EX menu table (402 items).
* `web/` — remote control (HTML/JS, installable as a PWA); `panel.html` — server control panel.

Some meter calibrations (PO, SWR, ALC) and WIDTH passband tables are approximate and need verification
on a real transceiver. Simulator and packaged-application checks do not replace hardware testing.
When first connecting a real FTX-1, verify PTT and the modulation source using a dummy load.
