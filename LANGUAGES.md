# English / Russian interface

Open **Settings → Language** in the remote control, or **Language** in the server control panel. Select **English** or **Русский**. The choice is stored in `config.json` as `"language": "en"` or `"language": "ru"`; existing configurations continue to use Russian until changed.

This is a station-wide setting: all connected control panels reconnect when it changes. Stop transmission, automatic transmit/reply modes and memory operations first. Receive audio briefly stops while the pages reconnect. The server rejects a language change during active operations.

The setting covers the main interface, server panel, controls, tooltips, help, notifications, server diagnostics and new AI explanations (CW, RTTY and NAVTEX). Existing AI explanations and historical log entries keep the language in which they were created. Callsigns, radio messages, CAT commands, imported data, user-written text and file formats are not translated. The CYR decoder option remains available for receiving Cyrillic Morse.

## Running the modified source

Install Python 3.10 or newer, then run:

```sh
python -m pip install -r requirements.txt
python server/main.py
```

On Windows, double-click `FTX1-Pult.pyw`. Build Windows executables with the existing `build_exe.bat` or GitHub Actions workflow; the entire `web` directory, including translations, is already included by the build configuration. Language switching does not require a separate English executable.

To test without radio hardware:

```sh
python server/main.py --sim
```

## Maintaining translations

`web/locales/en.json` is the shared catalogue for Python and JavaScript. Russian source strings are keys and the fallback language. Keep placeholders (`%s`, `%d`, Python `{name}` fields), HTML structure and protocol identifiers unchanged.

- In Python, wrap application-owned literals with `tr(...)` from `server/i18n.py`. Translate stored constants at their point of use, after loading settings.
- In JavaScript, use `I18n.t('source')` or the tagged template syntax `` I18n.t`source ${value}` ``. Interpolated values are preserved verbatim.
- Static HTML text and human-facing attributes (`title`, `placeholder`, `aria-label`, `alt`, `data-label`, `data-opts`) are localized once before application startup. Live DOM content and input values are never scanned for translation.
- AI response keys such as `ru` are retained for wire compatibility; in English mode their values contain English explanations.
- Add new locale assets to the service-worker cache and keep them inside `web` for executable packaging.

## Checks

```sh
python -m unittest discover -s tests -v
npm ci --prefix tests/web
npm test --prefix tests/web
```

The frontend checks execute the actual application scripts in a DOM environment with mocked canvas and WebSocket APIs. They cover English/Russian startup, static controls and tooltips, dynamic message catalogue coverage, storage failures and radio/user text preservation. Python checks cover formatting, model prompts, persistence, authentication, failed saves, language notifications and transmission guards.

Validate the final Windows executable and hardware behavior on a real FTX-1 before general release.

## Closing the application (2.10.1)

The window X button closes the panel and keeps the server in the system tray. Use the **Exit** button at the top or bottom of the server panel, or **Exit** in the tray menu, to stop the application completely. Pending panel settings are saved before Exit; if saving fails, the panel stays open and reports the error.

Shutdown cancels radio jobs while their handles are still available, releases PTT, closes client connections and listeners, and removes the tray icon. A pending LM Studio response no longer holds the process open. The Windows build checks Exit during a deliberately blocked model request, settings persistence and restart on the same port; its result is included as `lifecycle.txt`.
