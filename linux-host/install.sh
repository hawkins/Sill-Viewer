#!/bin/bash
# Installs Sill for Linux for this user: packages, the app, and a service that starts at login.
# Ubuntu/Debian with GNOME, or Arch with GNOME or Hyprland.
set -e
cd "$(dirname "$0")"

hyprland=0
case "${XDG_CURRENT_DESKTOP:-}" in *Hyprland*) hyprland=1 ;; esac
[ -n "${HYPRLAND_INSTANCE_SIGNATURE:-}" ] && hyprland=1

ask() {     # ask "Question?" y|n  (the default)
    local a hint="[y/N]"
    [ "$2" = y ] && hint="[Y/n]"
    read -r -p "$1 $hint " a </dev/tty || a=""
    [ -z "$a" ] && a="$2"
    case "$a" in [yY]*) return 0 ;; *) return 1 ;; esac
}

echo "==> Installing packages (asks for your password)"
if command -v apt-get >/dev/null; then
    sudo apt-get update -qq
    sudo apt-get install -y \
        python3-gi python3-gi-cairo python3-pil python3-openssl python3-cryptography python3-zeroconf librsvg2-common \
        gir1.2-gstreamer-1.0 gir1.2-gst-plugins-base-1.0 \
        gstreamer1.0-tools gstreamer1.0-plugins-base gstreamer1.0-plugins-good \
        gstreamer1.0-plugins-bad gstreamer1.0-plugins-ugly gstreamer1.0-pipewire \
        libnotify-bin qrencode avahi-daemon iproute2 gir1.2-gtk-4.0 \
        gir1.2-gtk-3.0 gir1.2-ayatanaappindicator3-0.1
    if [ $hyprland = 1 ]; then
        sudo apt-get install -y python3-evdev wl-clipboard grim xdg-desktop-portal xdg-desktop-portal-hyprland ||
            echo "Some Hyprland packages are missing; sill-linux check shows what does not work."
    fi
elif command -v pacman >/dev/null; then
    pkgs=(python python-gobject python-cairo python-pillow librsvg python-pyopenssl python-cryptography python-zeroconf
          gstreamer gst-plugins-base gst-plugins-good gst-plugins-bad gst-plugin-pipewire
          libnotify qrencode avahi iproute2 gtk4 gtk3 libayatana-appindicator
          ffmpeg gcc pkgconf)
    [ $hyprland = 1 ] && pkgs+=(python-evdev wl-clipboard grim xdg-desktop-portal xdg-desktop-portal-hyprland)
    sudo pacman -S --needed "${pkgs[@]}"
else
    echo "No apt-get or pacman here. Install by hand: Python 3 with PyGObject, pyOpenSSL, cryptography and"
    echo "zeroconf; GStreamer with the base, good and bad plugins and the PipeWire plugin; libnotify, qrencode,"
    echo "Avahi, GTK 3 and 4, Ayatana AppIndicator; on Hyprland also python-evdev, wl-clipboard and"
    echo "xdg-desktop-portal-hyprland. Then run this again."
    exit 1
fi

echo "==> Installing the app"
APP="$HOME/.local/share/sill-linux/app"
rm -rf "$APP"
mkdir -p "$APP" "$HOME/.local/bin" "$HOME/.config/systemd/user"
cp -r sill_linux "$APP/"
cat > "$HOME/.local/bin/sill-linux" <<EOF
#!/bin/sh
PYTHONPATH="$APP" exec /usr/bin/python3 -m sill_linux "\$@"
EOF
chmod +x "$HOME/.local/bin/sill-linux"

# The Vulkan encoder helper comes built for Ubuntu's FFmpeg. Elsewhere, build it for this FFmpeg
# (without it, NVIDIA still encodes through NVENC).
VKENC="$APP/sill_linux/vkenc/sill-vkenc"
if ldd "$VKENC" 2>/dev/null | grep -q "not found"; then
    if command -v gcc >/dev/null && pkg-config --exists libavcodec libavutil 2>/dev/null; then
        "$APP/sill_linux/vkenc/build.sh" >/dev/null && echo "built the Vulkan encoder helper for this FFmpeg" \
            || echo "could not build the Vulkan encoder helper; NVENC or VA-API encode instead"
    fi
fi

if [ $hyprland = 1 ]; then
    echo "==> Hyprland"
    if [ ! -w /dev/uinput ]; then
        echo "Sill types and clicks through a virtual mouse and keyboard (/dev/uinput), which this user"
        echo "cannot open yet."
        if ask "Let the input group use /dev/uinput, and add $USER to it? (asks for your password)" y; then
            echo 'KERNEL=="uinput", GROUP="input", MODE="0660", OPTIONS+="static_node=uinput"' |
                sudo tee /etc/udev/rules.d/60-sill-uinput.rules >/dev/null
            echo uinput | sudo tee /etc/modules-load.d/sill-uinput.conf >/dev/null
            sudo modprobe uinput
            sudo udevadm control --reload-rules
            sudo udevadm trigger /dev/uinput 2>/dev/null || true
            sudo usermod -aG input "$USER"
            echo "Log out and back in once, so the new group counts."
        fi
    fi
    # xdg-desktop-portal-hyprland asks on this screen which monitor to share. sill-linux-picker
    # answers for Sill (and shows Hyprland's own picker to every other app).
    XDPH="${XDG_CONFIG_HOME:-$HOME/.config}/hypr/xdph.conf"
    PICKER="$APP/sill_linux/sill-linux-picker"
    if grep -q "sill-linux-picker" "$XDPH" 2>/dev/null; then
        :
    elif grep -q "custom_picker_binary" "$XDPH" 2>/dev/null; then
        echo "$XDPH already sets custom_picker_binary. Without Sill's picker, pick each monitor"
        echo "once on this screen when a device first shows it."
    else
        echo "The screen sharing portal asks on this screen which monitor to share. Sill can answer"
        echo "it by itself (other apps still get Hyprland's picker)."
        if ask "Add Sill's picker to $XDPH?" y; then
            mkdir -p "$(dirname "$XDPH")"
            printf '\n# Added by Sill for Linux: answers the picker for Sill, shows hyprland-share-picker to other apps.\nscreencopy {\n    custom_picker_binary = %s\n}\n' \
                "$PICKER" >> "$XDPH"
            systemctl --user restart xdg-desktop-portal-hyprland 2>/dev/null || true
        else
            echo "Then pick each monitor once on this screen when a device first shows it."
        fi
    fi
fi

if ! systemctl is-active --quiet avahi-daemon 2>/dev/null; then
    echo "==> Bonjour"
    echo "Sill finds this computer through Bonjour (mDNS). The Avahi daemon is off; without it, Sill"
    echo "advertises by itself (python-zeroconf)."
    if ask "Turn on the Avahi daemon? (asks for your password)" y; then
        sudo systemctl enable --now avahi-daemon
    fi
fi

cat > "$HOME/.config/systemd/user/sill-linux.service" <<'EOF'
[Unit]
Description=Sill host: stream this desktop to the Sill app on iPhone and iPad
After=graphical-session.target
PartOf=graphical-session.target

[Service]
ExecStart=%h/.local/bin/sill-linux run
Restart=always
RestartSec=3

[Install]
WantedBy=graphical-session.target
EOF

cat > "$HOME/.config/systemd/user/sill-linux-tray.service" <<'UNIT'
[Unit]
Description=Sill tray icon
After=graphical-session.target sill-linux.service
PartOf=graphical-session.target

[Service]
ExecStart=%h/.local/bin/sill-linux tray
Restart=on-failure
RestartSec=3

[Install]
WantedBy=graphical-session.target
UNIT

echo "==> Checking this computer"
"$HOME/.local/bin/sill-linux" check || true

echo "==> Starting the service"
systemctl --user daemon-reload
echo "Sill can start whenever you log in, or only when you ask (a Sill shortcut in your application menu"
echo "and on the Desktop, and  sill-linux start). While it runs, a paired device can connect."
if ask "Start Sill when you log in?" y; then
    login=1
    systemctl --user enable sill-linux.service sill-linux-tray.service
    systemctl --user restart sill-linux.service sill-linux-tray.service
    sleep 2
    systemctl --user --no-pager status sill-linux.service | head -5
else
    login=0
    "$HOME/.local/bin/sill-linux" startup manual
    systemctl --user stop sill-linux.service sill-linux-tray.service 2>/dev/null || true
    echo "Sill is not running now: start it from the Sill shortcut, or  sill-linux start."
fi
if [ $login = 1 ] && ! systemctl --user is-active --quiet graphical-session.target; then
    echo "NOTE: graphical-session.target is not active, so the service will not start at login by itself."
    echo "      Start your desktop with a session manager that starts it (uwsm, for one), or start Sill from"
    echo "      your desktop's autostart: systemctl --user start sill-linux sill-linux-tray"
fi
if [ $hyprland = 1 ] && ! systemctl --user show-environment | grep -q '^HYPRLAND_INSTANCE_SIGNATURE='; then
    echo "NOTE: the systemd user session does not know about Hyprland yet. Add this to your Hyprland autostart:"
    echo "      dbus-update-activation-environment --systemd WAYLAND_DISPLAY XDG_CURRENT_DESKTOP HYPRLAND_INSTANCE_SIGNATURE"
fi

echo "==> Updates"
echo "Sill shows when an update is available. You choose when to install it."
if ask "Install updates by themselves instead, every hour?" n; then ./update.sh --install; else ./update.sh --setup; fi

if systemctl is-enabled --quiet gdm 2>/dev/null || systemctl is-enabled --quiet gdm3 2>/dev/null; then
    echo "==> Display watchdog (restarts GDM if the desktop gets stuck on a black screen)"
    display-watchdog/install.sh
fi

echo
if [ $login = 1 ]; then
    echo "Done. On your iPad, open Sill. Tap this computer. Then type the code from the notification."
else
    echo "Done. Start Sill, then on your iPad open Sill, tap this computer, and type the code from the notification."
fi
echo "Log:  journalctl --user -u sill-linux -f"
