# Running LLM Wiki as a Windows service

`llmwiki open` runs the API (port 8000) and the web app (port 3000) in a console window and logs every HTTP request. The web page polls about once a second, so that console fills with identical `GET .../documents` lines.

This guide installs the same two servers as a **Windows service**: they start with Windows (Automatic, or Automatic (Delayed Start)), show no window, and log to a file whose verbosity you set in a config file. It also covers removing the service.

How it fits together:

- **[WinSW](https://github.com/winsw/winsw)** (one `.exe` plus an `.xml` file) is the actual service. Python can't talk to the Windows service manager by itself.
- WinSW runs **`windows\llmwiki_background.py`**, the launcher. It reads `background.toml`, waits for the wiki folder to exist, starts both servers, restarts one that crashes, and writes `llmwiki.log`. When the service stops, Windows also kills both servers.
- The **MCP server** that Claude uses is separate: the Claude app starts it. This service doesn't change it.

The steps use these placeholders. Replace them with your own paths everywhere:

| Placeholder | Meaning | Example |
|---|---|---|
| `<repo>` | the llmwiki checkout | `D:\Projects\llmwiki` |
| `<svc>` | a new folder for the service's files (outside the repo) | `D:\Projects\llmwiki-service` |
| `<workspace>` | the wiki folder | `G:\llmdata` |
| `<index>` | the search index folder (`LLMWIKI_INDEX_PATH` in `claude_desktop_config.json`) | `C:\Users\<you>\AppData\Local\LLMWiki\indexes\llmdata` |

Before you start, the repo must be set up: the venv with the requirements installed, `npm install` in `web\`, and the workspace initialized (it has an index).

## Set up the service

Steps 6 to 9 need an **elevated PowerShell** (right-click PowerShell, *Run as administrator*). The others work in a normal one.

### 1. Stop `llmwiki open`

Close its window (or `start-llmwiki.bat`). The service and `llmwiki open` can't run at the same time, because both need ports 8000 and 3000.

**Check:** this prints nothing:

```powershell
Get-NetTCPConnection -State Listen -LocalPort 8000, 3000 -ErrorAction SilentlyContinue
```

### 2. Create the service folder and get WinSW

1. Create `<svc>`, e.g. `New-Item -ItemType Directory 'D:\Projects\llmwiki-service'`.
2. From the [WinSW v2.12.0 release](https://github.com/winsw/winsw/releases/tag/v2.12.0), download **`WinSW.NET461.exe`**. Windows 10 and 11 already include the .NET Framework it needs. If a machine has no .NET Framework 4.6.1+, use `WinSW-x64.exe` instead.
3. Save it in `<svc>` and rename it to **`llmwiki-service.exe`**.
4. Remove the "downloaded from the internet" mark: `Unblock-File '<svc>\llmwiki-service.exe'`.

### 3. Create the launcher config

```powershell
Copy-Item '<repo>\windows\background.example.toml' '<svc>\background.toml'
notepad '<svc>\background.toml'
```

Set, with full paths:

```toml
workspace = '<workspace>'
index_path = '<index>'

[log]
dir = '<svc>\logs'
```

Keep the single quotes, which keep the backslashes intact. The `[log]` section has the other logging switches (see [Logs](#logs)); the defaults are quiet.

### 4. Test the launcher in a terminal

```powershell
& '<repo>\.venv\Scripts\python.exe' '<repo>\windows\llmwiki_background.py' --config '<svc>\background.toml' --foreground
```

**Check:** you see `[launcher] started api` and `[launcher] started web`, and http://localhost:3000 shows the wiki after a few seconds. The first page load compiles in dev mode, so allow up to a minute. Then press **Ctrl+C**.

If it fails here, fix the cause first (see [Troubleshooting](#troubleshooting)). The service runs exactly this command.

### 5. Write the service definition

Create `<svc>\llmwiki-service.xml`. It must have the same base name as the `.exe` and sit in the same folder. Replace the placeholders:

```xml
<service>
  <id>llmwiki</id>
  <name>LLM Wiki</name>
  <description>LLM Wiki API (port 8000) and web app (port 3000).</description>

  <executable><repo>\.venv\Scripts\python.exe</executable>
  <arguments>"<repo>\windows\llmwiki_background.py" --config "%BASE%\background.toml"</arguments>
  <workingdirectory><repo></workingdirectory>

  <startmode>Automatic</startmode>
  <onfailure action="restart" delay="30 sec"/>
  <resetfailure>1 hour</resetfailure>

  <!-- Stop sends Ctrl+C to the launcher first, so it shuts both servers down cleanly. -->
  <stopparentprocessfirst>true</stopparentprocessfirst>
  <stoptimeout>15 sec</stoptimeout>

  <!-- Services don't see your user PATH. Add Node.js explicitly if it isn't on the system PATH. -->
  <env name="PATH" value="C:\Program Files\nodejs;%PATH%"/>

  <!-- WinSW's own capture of the launcher's console output (normally almost empty). -->
  <logpath>%BASE%\logs</logpath>
  <log mode="roll-by-size">
    <sizeThreshold>1024</sizeThreshold>
    <keepFiles>3</keepFiles>
  </log>
</service>
```

`%BASE%` means "the folder this XML is in", so the config and logs stay in `<svc>`.

### 6. Install the service (elevated)

```powershell
& '<svc>\llmwiki-service.exe' install
```

**Check:** `Get-Service llmwiki | Select-Object Status, StartType, DisplayName` shows `Stopped  Automatic  LLM Wiki`.

### 7. Choose the start type (elevated)

- **Automatic** (a normal folder as workspace): nothing to do. It's the default from the XML.
- **Automatic (Delayed Start)** (for example on the PC with the Cryptomator vault):

  ```powershell
  sc.exe config llmwiki start= delayed-auto
  ```

  Type `sc.exe`, not `sc`: in PowerShell `sc` means `Set-Content`. The space after `start=` is required.

**Check:** `sc.exe qc llmwiki` shows `START_TYPE : 2 AUTO_START` or `2 AUTO_START (DELAYED)`.

Either way, the launcher waits for the workspace folder to appear. A service that starts before the vault is unlocked just waits, and starts the servers once the folder exists.

### 8. Choose the account it runs as

**LocalSystem** (the default) is fine when the workspace and index are ordinary folders on a local disk. Nothing to do.

**A Cryptomator vault needs one change, and LocalSystem still works.** A service runs outside your desktop session, so it never sees the vault's drive letter: with `workspace = 'G:\llmdata'` the log keeps saying `waiting for G:\llmdata`. The vault's network path is visible to the service, though:

1. With the vault unlocked, get its network path (here `G:` is the vault's drive letter):

   ```powershell
   (Get-PSDrive G).DisplayRoot
   ```

   It prints something like `\\cryptomator-vault\Ab3dEf9hIj2_\MyVault`. The middle part is the vault's ID from Cryptomator's settings, so it stays the same across reboots and unlocks. It changes only if the vault is removed and added again in Cryptomator.

2. In `<svc>\background.toml`, use that path plus the wiki subfolder, e.g. `workspace = '\\cryptomator-vault\Ab3dEf9hIj2_\MyVault\llmdata'`.

This was tested on 2026-10-20: running as LocalSystem, the service read all pages and the web app worked, with no password stored. The vault still has to be unlocked; until then the launcher waits.

**Your own account** is the fallback if a workspace is visible only to you:
1. Open `services.msc`, double-click **LLM Wiki**, and go to the **Log On** tab.
2. Pick **This account**, enter `.\<your user name>` and your Windows password twice, and click **OK**. Windows grants the "Log on as a service" right and says so.

### 9. Start it (elevated)

```powershell
Start-Service llmwiki
```

**Check:**

- `Get-Service llmwiki` shows `Running`.
- `Get-Content '<svc>\logs\llmwiki.log' -Tail 20` shows `[launcher] started api` and `[launcher] started web`.
- http://localhost:3000 shows the wiki.

From now on it starts with Windows.

## Logs

| File | What's in it |
|---|---|
| `<svc>\logs\llmwiki.log` (+ `.1`, `.2`, ...) | The launcher's log, set by `background.toml`. Lines are tagged `[launcher]` (start, restart, waiting for the folder), `[api]` and `[web]`. |
| `<svc>\logs\llmwiki-service.out.log` / `.err.log` | WinSW's capture of the launcher's own console output. Normally almost empty. Look here when `llmwiki.log` is empty or missing, e.g. when Python itself can't start. |

To follow the log live: `Get-Content '<svc>\logs\llmwiki.log' -Tail 30 -Wait`

Settings in `background.toml`:

| Setting | Effect |
|---|---|
| `log.level` | API and app messages at or above this level. `"warning"` (default) is quiet; `"info"` adds startup lines and the file watcher's re-index messages; `"debug"` is everything. |
| `log.access` | One line per HTTP request: the flood `llmwiki open` shows. `false` by default. |
| `log.web` | Next.js output. `false` by default; turn it on to debug the web app. |
| `log.max_mb`, `log.backups` | Size at which `llmwiki.log` rotates, and how many old copies to keep. |
| `web.enabled`, `web.mode` | Run the web app or only the API; `"dev"` (like `llmwiki open`) or `"start"` (production; needs `npm run build` in `web\`). |

**After changing a setting**, restart the service (elevated): `Restart-Service llmwiki`

## Day-to-day (elevated, except status)

| Task | Command |
|---|---|
| Status | `Get-Service llmwiki` |
| Stop / start / restart | `Stop-Service llmwiki` / `Start-Service llmwiki` / `Restart-Service llmwiki` |
| Stop starting with Windows | `Set-Service llmwiki -StartupType Manual` |
| Start with Windows again | `Set-Service llmwiki -StartupType Automatic` (then `sc.exe config llmwiki start= delayed-auto` for delayed) |
| After `git pull` | `Restart-Service llmwiki`. With `web.mode = "start"`, run `npm run build` in `web\` first. |
| Use `llmwiki open` instead for a while | `Stop-Service llmwiki` first |

## Remove the service

In an elevated PowerShell:

1. **Stop and uninstall it:**

   ```powershell
   & '<svc>\llmwiki-service.exe' stop
   & '<svc>\llmwiki-service.exe' uninstall
   ```

   If `llmwiki-service.exe` is already gone, use Windows' own tool instead:

   ```powershell
   sc.exe stop llmwiki
   sc.exe delete llmwiki
   ```

2. **Check:** `Get-Service llmwiki` now fails with "Cannot find any service with service name 'llmwiki'". If the service still shows as "marked for deletion", close `services.msc` (and Task Manager's Services tab) and check again.

3. **Optional:** delete the `<svc>` folder, which holds only the WinSW files, `background.toml` and the logs. The repo, the wiki and the index are not touched.

4. If you still want the web app, go back to `llmwiki open` or `start-llmwiki.bat`.

## Troubleshooting

Look at `<svc>\logs\llmwiki.log` first, then `llmwiki-service.err.log`.

| Symptom or log line | Meaning and fix |
|---|---|
| `port 8000 is already in use` | `llmwiki open` or another copy is running. Close it, then `Restart-Service llmwiki`. |
| `waiting for <workspace>` | The folder doesn't exist for the service: the vault is locked, or `workspace` uses a drive letter the service can't see. Use the vault's network path (step 8). |
| `no index at ...` | `index_path` is wrong, or the workspace was never initialized: run `<repo>\.venv\Scripts\python.exe <repo>\llmwiki init <workspace>` once. |
| `web dependencies missing` | Run `npm install` in `<repo>\web`. |
| `api exited with code ...; restarting` | The API crashed. Set `log.level = "info"`, restart the service, and read why. Restarts back off up to 5 minutes. |
| `web exited ...` right after start, `'node' is not recognized` in `.err.log` | Node.js isn't on the service's PATH: fix the `<env name="PATH" .../>` line in the XML, then `Restart-Service llmwiki`. |
| `install` says "Access is denied" | The PowerShell isn't elevated. |
| Service starts, then stops at once | `background.toml` has an error (bad path, bad value). The exact message is in `llmwiki.log`, or in `.err.log` if the log folder couldn't be created. |
