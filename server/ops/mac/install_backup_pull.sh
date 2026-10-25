#!/bin/bash
# Sets up the hourly pull of the box's nightly dump on this Mac - user level,
# no admin rights: copies pull_backup.sh to ~/Developer/wifi-audit-backup-pull/
# and loads a LaunchAgent (~/Library/LaunchAgents/com.wifi-audit.backup-pull.plist)
# that runs it at login and every hour while the Mac is awake. Log:
# ~/Library/Logs/wifi-audit-backup-pull.log. Dumps: ~/wifi-audit-backups/nightly/.
#
#   server/ops/mac/install_backup_pull.sh            # install / update
#   server/ops/mac/install_backup_pull.sh remove     # unload and delete the agent (keeps the dumps)
set -euo pipefail
LABEL=com.wifi-audit.backup-pull
DIR=$HOME/Developer/wifi-audit-backup-pull
PLIST=$HOME/Library/LaunchAgents/$LABEL.plist
LOG=$HOME/Library/Logs/wifi-audit-backup-pull.log
DOMAIN=gui/$(id -u)

launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
if [[ ${1:-} == remove ]]; then
  rm -f -- "$PLIST" && echo "removed $LABEL (the script in $DIR and the dumps stay)"
  exit 0
fi
mkdir -p "$DIR" "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
cp "$(dirname "$0")/pull_backup.sh" "$DIR/pull_backup.sh"
chmod 755 "$DIR/pull_backup.sh"
cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array><string>$DIR/pull_backup.sh</string></array>
  <key>RunAtLoad</key><true/>
  <key>StartInterval</key><integer>3600</integer>
  <key>StandardOutPath</key><string>$LOG</string>
  <key>StandardErrorPath</key><string>$LOG</string>
  <key>ProcessType</key><string>Background</string>
  <key>LowPriorityIO</key><true/>
</dict>
</plist>
PLIST
plutil -lint "$PLIST" >/dev/null
launchctl bootstrap "$DOMAIN" "$PLIST"
echo "loaded $LABEL: hourly + at login; log $LOG"
