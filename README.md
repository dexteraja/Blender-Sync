# Blender-Sync
Made By Love

# BlenderSync Setup Guide

A comprehensive, step-by-step guide to installing and configuring BlenderSync across your local environment, Blender, and Roblox Studio.

## Prerequisites

* Python (Latest Version): Ensure Python is installed and added to your system's PATH environment variable. If necessary, acquire the installer from [python.org](https://python.org)

## Installation & Execution

### 1. Repository Setup

* Extract the downloaded source archive (.rar).
* Open a terminal instance and navigate to the project root directory:
  ```
  cd blendersync
  ```

### 2. Server Initialization

* Install the required dependencies (mandatory upon initial execution):
  ```
  python -m pip install -r requirements.txt
  ```
* Launch the local server process:
  ```
  python server.py
  ```
* By default the server listens on **port 5000** on all network interfaces. Keep this terminal open while you work — both Blender and Roblox Studio talk to this process.

### 3. Blender Integration

* Launch Blender.
* Navigate to **Edit > Preferences > Add-ons**.
* Select **Install...** at the upper-right corner, locate **live_link_sync.py**, and proceed with installation.
* Enable the BlenderSync add-on by ticking its corresponding checkbox.
* In the 3D Viewport, press **N** to toggle the sidebar menu and access the BlenderSync panel interface.
* Set **Server URL** to where `server.py` is running (`http://localhost:5000` if it's the same machine). If you've enabled auth (see [Security & Sharing](#security--sharing) below), also fill in **Shared Secret**.

### 4. Roblox Studio Integration

`BlenderLiveLink.lua` is a **Studio plugin**, not a game script — it uses plugin-only APIs (toolbar buttons, `plugin:CreateToolbar()`) that only run in Studio itself, not in ServerScriptService at runtime. Install it as a plugin:

* Locate your local Roblox plugins folder:
  * **Windows:** `%LOCALAPPDATA%\Roblox\Plugins`
  * **macOS:** `~/Documents/Roblox/Plugins`
  * (Or from Studio's ribbon: **Plugins > Plugins Folder**, if available in your version.)
* Copy **BlenderLiveLink.lua** into that folder.
* If you've enabled auth (see below), open the file first and set the `SHARED_SECRET` constant near the top to match your server before copying it in.
* Restart Roblox Studio, or reload plugins from **Plugins > Manage Plugins**.
* Open your place file and make sure **Allow HTTP Requests** is enabled: **Game Settings > Security**.
* A **"Blender Live Link"** toolbar button will appear — click it to start polling. Synced objects show up under a `BlenderLiveLink` folder in Workspace.

## Security & Sharing

`server.py` binds to all network interfaces and, out of the box, accepts requests from **anyone who can reach the port** — fine for solo local use, not fine the moment you share it with someone else on the same network. If you're setting this up for more than just yourself on your own machine:

* **Set a shared secret.** Start the server with:
  ```
  # macOS/Linux
  LIVELINK_SHARED_SECRET="pick-a-long-random-string" python server.py

  # Windows (PowerShell)
  $env:LIVELINK_SHARED_SECRET="pick-a-long-random-string"; python server.py
  ```
  Then set the same value in the Blender panel's **Shared Secret** field, and in `BlenderLiveLink.lua`'s `SHARED_SECRET` constant. Share the secret itself over a private channel (DM, not a public repo/README) — never commit it to source control.
* **Stick to trusted networks.** Traffic is plain HTTP (unencrypted). This is fine on your own LAN, but don't port-forward this server to the open internet — if you need remote access, put it behind something like Tailscale or a Cloudflare Tunnel rather than exposing port 5000 directly.
* A few other built-in limits, in case you're tuning them:

  | Variable | Default | Purpose |
  |---|---|---|
  | `LIVELINK_SHARED_SECRET` | *(unset — no auth)* | Requires a matching `X-LiveLink-Token` header on every endpoint except `/` and `/health`. |
  | `LIVELINK_MAX_CONTENT_LENGTH` | 50 MB | Rejects oversized requests before they're read into memory. |
  | `LIVELINK_RATE_LIMIT_MAX` / `LIVELINK_RATE_LIMIT_WINDOW` | 120 req / 60s | Basic per-IP rate limit on `POST`/`DELETE`. |

Inspired by : **rungkadkacaw**
