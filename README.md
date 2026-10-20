# pc-power-agent

A tiny Windows background app that lets Home Assistant put the PC to **sleep** or **shut it down**.

It connects to your MQTT broker (for example the Mosquitto add-on) and uses MQTT discovery to add a
device with three entities:

- `button.<name>_sleep`: puts the PC to sleep
- `button.<name>_shut_down`: normal Windows shutdown (apps are asked to close, nothing is forced)
- `binary_sensor.<name>_online`: on while the PC is running and connected
- `binary_sensor.<name>_active`: on while someone used the keyboard or mouse in the last
  `idle_after_seconds` (default 300)
- `binary_sensor.<name>_camera_in_use` / `binary_sensor.<name>_microphone_in_use`: on while any
  app is using the webcam or microphone (read from Windows' privacy usage records)
- `sensor.<name>_mac_address`: the MAC of the network adapter, for setting up Wake-on-LAN

It runs as a small icon in the taskbar's notification area. The screen of the monitor icon shows
the state: green means connected, orange means connecting, grey means stopped. Right-click it for:

- **Start** / **Stop**: connect to or disconnect from Home Assistant. While stopped, the buttons
  show as unavailable in Home Assistant.
- **Open folder**: opens the folder with `config.json` and the log `pc_power.log`.
- **Exit**: stops the agent and closes it.

Run it with `--no-tray` to run it without the icon.

## Install

1. Download `pc_power.exe` from the latest [release](../../releases/latest) and put it in a
   folder such as `C:\pc-power`.
2. Double-click it once. It writes a `config.json` template next to itself and exits.
3. Edit `config.json`:
   - `host`: your Home Assistant / MQTT broker IP address
   - `username` / `password`: your MQTT login
   - `device_name`: the name shown in Home Assistant (defaults to the PC name)
   - `idle_after_seconds`: how long without input before the PC counts as idle (default 300)
   - `shutdown_delay_seconds`: 0 shuts down right away. Something like 30 gives you time to cancel with `shutdown /a`.
4. Double-click it again. The device should show up in Home Assistant under
   Settings > Devices > MQTT within a few seconds.
5. Make it start at logon. Run this once in a terminal:

   ```
   schtasks /create /tn "PC power agent" /sc onlogon /rl limited /tr "C:\pc-power\pc_power.exe"
   ```

To stop it, use **Exit** in the tray menu. To remove the logon task, run
`schtasks /delete /tn "PC power agent" /f`.

Windows SmartScreen may warn the first time because the exe isn't signed. Click
**More info**, then **Run anyway**.

## Notes

- **Sleep turns into hibernate:** if hibernation is on, Windows may hibernate instead of
  sleeping. Run `powercfg /h off` as admin to always get a real sleep.
- **Waking the PC:** this agent can't wake the PC. Use Home Assistant's Wake on LAN integration with
  the MAC from the MAC address sensor, and enable Wake-on-LAN in the BIOS and the network adapter settings.
- **Who can control it:** anyone who can publish to `pc_power/<name>/command` can sleep or shut
  down the PC, so keep your broker password-protected.
- **Topics:** commands go to `pc_power/<name>/command` (`sleep` or `shutdown`) and status goes to
  `pc_power/<name>/status` (`online`/`offline`, retained, with a last-will message).

## Build it yourself

```
pip install -r requirements.txt pyinstaller
pyinstaller --onefile --noconsole --name pc_power pc_power.py
```

GitHub Actions builds the exe on every push. Every push to `main` also publishes it as a new release.
