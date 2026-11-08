# Sill for Linux

> Unofficial: an independent companion to [Sill](https://getsill.app), not made by the Sill team.

A Linux host for the [Sill](https://github.com/Saffsanity/sill) iPhone and iPad app. Your Linux
desktop shows up in the Sill app from the App Store, like a Mac does.

Needs: Ubuntu or Arch, with GNOME on Wayland or Hyprland. An NVIDIA, AMD or Intel GPU for hardware
HEVC (software works, slower).

`install.sh` uses `apt-get` or `pacman`, whichever the computer has.

## Install

```
tar xzf sill-linux.tar.gz
cd sill-linux
./install.sh
```

## Use

1. On the iPad, open Sill. Your computer shows in the list.
2. Tap it. A notification with a 12-digit code shows on the computer.
3. On the iPad, choose **Enter Code Instead** and type the code.

You pair one time. After that, tap and go.

If the computer's screen is locked, connecting unlocks it (GNOME shows nothing to a screen share
while locked). Only a device you paired can connect, so it needs no password.

## What you can pick on the iPad

- **The desktop:** your main monitor.
- **The bar:** each monitor, plus **Virtual Display**, a new screen sized to the iPad.
- **Settings panel:** Quality (up to 150 Mbps), 60 or 120 fps, Retina or Standard,
  and **Virtual display**: when on, "Desktop" is a screen sized to the iPad.
- **Fit screen / Match this screen (Sill Viewer):** the real monitor switches to its resolution
  closest to the viewer window (or the viewer's whole screen), and back when you turn it off or
  disconnect. A GNOME virtual screen would fit exactly, but on some GPUs (NVIDIA, GNOME 50) it
  draws only a few frames a second. To use it anyway: run the host with `--fit virtual`.

## On Hyprland

Everything above works the same, except what needs GNOME: virtual screens, Fit screen and Match this
screen (the viewer scales the monitor instead). Hyprland has no remote desktop API, so:

- **The screen** comes through the ScreenCast portal (xdg-desktop-portal-hyprland). Its picker would
  ask on this computer's screen which monitor to share. `install.sh` offers to add Sill's own picker
  to `~/.config/hypr/xdph.conf` (`screencopy:custom_picker_binary`): it answers for Sill and shows
  Hyprland's picker to every other app. Without it, pick each monitor once when a device first asks
  for it; the portal remembers the choice.
- **Windows and workspaces** are in the iPad's list next to the monitors. Tapping one shows it on
  the desktop (Hyprland focuses it, switching workspace if needed) and streams its monitor. The
  stream is the whole monitor, not that window alone.
- **Mouse and keyboard** are a virtual device (`/dev/uinput`), so your user must be in the `input`
  group (`install.sh` offers to set that up). Typed text is mapped for a US keyboard layout.
- **Clipboard** goes through wl-clipboard.
- **The tray icon** shows in a bar with a system tray, such as Waybar's `tray` module.
- The service starts with `graphical-session.target` (uwsm starts it). The systemd user session needs
  `WAYLAND_DISPLAY` and `HYPRLAND_INSTANCE_SIGNATURE`; `install.sh` says when they are missing.

## Commands

| Command | What it does |
|---|---|
| `sill-linux check` | Tests GNOME or Hyprland, PipeWire, the encoder and Avahi. |
| `sill-linux pair` | Shows a QR code and a code, to pair a device. |
| `sill-linux status` | Shows what is connected and streaming. |
| `sill-linux devices` | Lists paired devices. |
| `sill-linux forget NAME` | Removes a paired device. |
| `journalctl --user -u sill-linux -f` | The live log. |

Options go in the service file (`~/.config/systemd/user/sill-linux.service`, the `ExecStart` line):
`--name "My PC"`, `--port 7456`, `--command-key super`, `--cursor hidden`, `--open` (no pairing).

## How it works

- GNOME's own screen sharing and input API (`org.gnome.Mutter.ScreenCast` and
  `org.gnome.Mutter.RemoteDesktop`): no pop-up, no extra display driver, virtual monitors built in.
- PipeWire → GStreamer → NVENC (or VA-API, or x265) HEVC, no B-frames, low-latency settings.
- Sill's wire format inside TLS 1.3, both keys pinned, paired with Sill's own code or QR pairing.
- Bonjour through Avahi (`_sill._tcp`).
- If the capture or the encoder stops, the host starts it again by itself.

## Not done yet

Single app windows (GNOME has no API for that), the app list, Mac menus, trackpad gestures,
remote access from outside your network (Tailscale to the home port may work).

## Display watchdog (GNOME only; off until you turn it on)

On NVIDIA, a GNOME virtual screen can leave the desktop stuck: the monitor stays black and
gnome-shell logs `Failed to lock front buffer` about once a minute. On GDM, `install.sh` sets up a small
root service that can restart GDM when that keeps happening for 90 seconds or more.

- **Off by default.** Turn it on from the Sill icon (**Fix a stuck black screen**) or with
  `sill-linux watchdog on`. Each desktop user decides for their own desktop.
- **Warns first:** a notice with a **Don't restart** button, 2 minutes before it acts.
- **Pause** for 4 hours from the icon or `sill-linux watchdog pause`, while you debug.
- **No endless loop:** at most once an hour, and it gives up after 2 restarts in 24 hours.

Log: `journalctl -u sill-display-watchdog`.

## Uninstall

```
systemctl --user disable --now sill-linux
rm -rf ~/.local/share/sill-linux ~/.config/sill-linux ~/.local/bin/sill-linux ~/.config/systemd/user/sill-linux.service
```
