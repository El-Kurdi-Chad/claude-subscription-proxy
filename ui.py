"""Desktop UI for claude-subscription-proxy.

Edit every proxy setting, start/stop/restart the proxy, and watch what it does:
live log, recent requests (incl. the exact prompt sent to Claude), subscription
quota, a test console and an explanation of how the proxy works. German and
English, following the system language unless one is picked. Standard library
only (tkinter), no extra dependencies.

Settings are written to config.json next to proxy.py; proxy.py reads the same
file, so start.bat / run.sh pick up whatever you configure here.

Run: `.venv\\Scripts\\pythonw.exe ui.py` (or start-ui.bat).
"""

from __future__ import annotations

import json
import locale
import os
import queue
import re
import secrets
import socket
import subprocess
import sys
import threading
import time
import tkinter as tk
import urllib.error
import urllib.request
from tkinter import messagebox, ttk
from tkinter.scrolledtext import ScrolledText

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(HERE, "config.json")
PROXY_SCRIPT = os.path.join(HERE, "proxy.py")

MODELS = ["sonnet", "opus", "haiku"]
API_LABELS = {"openai": "OpenAI Chat", "responses": "Responses", "completions": "OpenAI Legacy",
              "anthropic": "Anthropic", "ollama": "Ollama"}
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")

# (key, default, kind). Keys are the env var names proxy.py reads; labels and
# help texts live in TEXTS as "f.<key>" and "h.<key>".
FIELDS: list[tuple[str, str, str]] = [
    ("PROXY_HOST", "127.0.0.1", "host"),
    ("PROXY_PORT", "3456", "int"),
    ("PROXY_API_KEY", "", "secret"),
    ("PROXY_AUTH_EXEMPT_LOCALHOST", "false", "bool"),
    ("PROXY_EXTRA_PORTS", "", "ports"),
    ("PROXY_DEFAULT_MODEL", "sonnet", "model"),
    ("CLAUDE_BIN", "claude", "entry"),
    ("PROXY_SUBPROCESS_TIMEOUT", "1800", "int"),
    ("PROXY_LOG_LEVEL", "INFO", "choice:DEBUG,INFO,WARNING,ERROR"),
    ("PROXY_QUOTA_WARN_RESET_MIN", "20", "int"),
    ("PROXY_QUOTA_HEAVY_TURNS", "120", "int"),
    ("PROXY_STATE_DIR", "~/.claude-subscription-proxy", "entry"),
]
FIELD_KEYS = [f[0] for f in FIELDS]
DEFAULTS = {f[0]: f[1] for f in FIELDS}


# ------------------------------------------------------------------- i18n ---

LANG_NAMES = {"de": "Deutsch", "en": "English"}

TEXTS: dict[str, dict[str, str]] = {
    "de": {
        "status.stopped": "Gestoppt",
        "status.running": "Läuft",
        "status.starting": "Startet …",
        "status.external": "Läuft (extern gestartet)",
        "status.crashed": "Abgestürzt (Code {code}) – siehe Log",
        "status.key_changed": "Läuft – API-Key geändert, bitte neu starten",
        "btn.start": "▶ Starten",
        "btn.stop": "■ Stoppen",
        "btn.restart": "⟳ Neustart",
        "btn.copy": "Kopieren",
        "header.base_url": "Base-URL (OpenAI):",
        "header.language": "Sprache:",
        "lang.auto": "Automatisch ({name})",
        "badge.local": "Nur dieser PC",
        "badge.net_key": "🔒 Netzwerk · mit API-Key",
        "badge.net_open": "⚠ Netzwerk · OHNE API-Key",
        "tab.settings": "Einstellungen",
        "tab.requests": "Anfragen",
        "tab.log": "Log",
        "tab.quota": "Quota",
        "tab.test": "Test",
        "tab.howto": "So funktioniert's",
        "settings.section": "Proxy",
        "f.PROXY_HOST": "Host",
        "h.PROXY_HOST": "127.0.0.1 = nur dieser PC. 0.0.0.0 = im lokalen Netzwerk erreichbar "
                        "(dann API-Key setzen!).",
        "f.PROXY_PORT": "Port",
        "h.PROXY_PORT": "Port, auf dem der Proxy lauscht.",
        "f.PROXY_API_KEY": "API-Key",
        "h.PROXY_API_KEY": "Leer = kein Schutz. Gesetzt = Clients müssen ihn als API-Key "
                           "mitschicken. Mehrere Keys mit Komma trennen.",
        "f.PROXY_AUTH_EXEMPT_LOCALHOST": "Lokal ohne Key",
        "h.PROXY_AUTH_EXEMPT_LOCALHOST": "Anfragen von diesem PC selbst brauchen keinen Key – "
                                         "nur Zugriffe aus dem Netzwerk.",
        "f.PROXY_EXTRA_PORTS": "Zusätzliche Ports",
        "h.PROXY_EXTRA_PORTS": "Optional, kommagetrennt. Z. B. 1234 (LM Studio) und 11434 (Ollama) "
                               "für Clients, die diese Ports fest erwarten. Belegte Ports werden "
                               "übersprungen.",
        "f.PROXY_DEFAULT_MODEL": "Standard-Modell",
        "h.PROXY_DEFAULT_MODEL": "Wird genutzt, wenn der Client kein (oder ein unbekanntes) Modell "
                                 "schickt. Alias (sonnet/opus/haiku) oder volle Modell-ID.",
        "f.CLAUDE_BIN": "Claude CLI",
        "h.CLAUDE_BIN": "Name oder voller Pfad der claude-CLI (muss eingeloggt sein).",
        "f.PROXY_SUBPROCESS_TIMEOUT": "Timeout (Sekunden)",
        "h.PROXY_SUBPROCESS_TIMEOUT": "Maximale Laufzeit einer einzelnen Anfrage.",
        "f.PROXY_LOG_LEVEL": "Log-Level",
        "h.PROXY_LOG_LEVEL": "DEBUG zeigt am meisten Details.",
        "f.PROXY_QUOTA_WARN_RESET_MIN": "Quota-Warnung: Minuten vor Reset",
        "h.PROXY_QUOTA_WARN_RESET_MIN": "So viele Minuten vor dem Reset des Nutzungsfensters bekommt "
                                        "das Modell die Anweisung, zum Ende zu kommen.",
        "f.PROXY_QUOTA_HEAVY_TURNS": "Quota-Warnung: ab Turns",
        "h.PROXY_QUOTA_HEAVY_TURNS": "Weiche Warnung ab dieser Anzahl Anfragen im 5-Stunden-Fenster.",
        "f.PROXY_STATE_DIR": "Status-Ordner",
        "h.PROXY_STATE_DIR": "Hier liegen quota.json und der leere Arbeitsordner der CLI.",
        "key.show": "Anzeigen",
        "key.hide": "Verbergen",
        "key.generate": "Generieren",
        "settings.autostart": "Proxy beim Öffnen der UI automatisch starten",
        "btn.save": "Speichern",
        "btn.save_restart": "Speichern & Neustart",
        "btn.defaults": "Standardwerte",
        "settings.saved_in": "Gespeichert in: {path}",
        "settings.dirty": "● Nicht gespeicherte Änderungen – erst nach „Speichern“ aktiv",
        "settings.clean": "✓ Alles gespeichert",
        "howto.section": "Client einrichten – Base-URL je nach Format",
        "howto.openai": "OpenAI-kompatibel (stio, Continue, Open WebUI …)",
        "howto.lmstudio": "LM Studio-Clients",
        "howto.responses": "OpenAI Responses API (Codex …)",
        "howto.anthropic": "Anthropic (Claude-SDKs, Claude Code …)",
        "howto.no_v1": "(ohne /v1)",
        "howto.ollama": "Ollama-Clients",
        "howto.remote": "Von anderen Geräten (+ Pfad wie oben)",
        "howto.key_set": "API-Key:  den Key aus „API-Key“ oben eintragen "
                         "(wird als Bearer-Token bzw. x-api-key gesendet)",
        "howto.key_local": "          Auf diesem PC geht es auch ohne Key.",
        "howto.key_none": "API-Key:  beliebig (z. B. x) – wird ignoriert",
        "howto.model": "Modell:   {model}  (oder {others}); unbekannte Namen wie gpt-4o → Standard-Modell",
        "howto.extra": "Zusatzports: {ports} – alle Formate sind auch dort erreichbar",
        "col.time": "Zeit",
        "col.api": "Format",
        "col.model": "Modell",
        "col.stream": "Stream",
        "col.msgs": "Msgs",
        "col.tools": "Tools",
        "col.tool_calls": "Tool-Calls",
        "col.duration": "Dauer",
        "col.status": "Status",
        "col.preview": "Letzte Nachricht",
        "requests.legend": "Gelb = läuft · Blau = Tool-Call erkannt · Rot = Fehler · Doppelklick zeigt, "
                           "was der Proxy an Claude geschickt hat. Die Liste lebt im Proxy und wird "
                           "beim Neustart geleert.",
        "yes": "ja",
        "no": "nein",
        "running": "läuft",
        "log.autoscroll": "Automatisch scrollen",
        "log.clear": "Leeren",
        "quota.section": "Abo-Nutzung",
        "q.status": "Status",
        "q.rate_limit_type": "Limit-Typ",
        "q.resets_at": "Fenster-Reset",
        "q.session_turns": "Anfragen im Fenster",
        "q.session_cost_usd": "API-Gegenwert (USD)",
        "q.session_started_at": "Fenster seit",
        "q.is_using_overage": "Nutzt Overage",
        "q.last_event_at": "Letztes Limit-Event",
        "quota.note": "Der USD-Wert ist nur der API-Gegenwert zur Orientierung – im Abo zahlst du ihn nicht.",
        "quota.in_min": "in {m} Min.",
        "quota.warn_from": "Warnung ab {n}",
        "test.format": "Format:",
        "test.model": "Modell:",
        "test.send": "Prompt senden",
        "test.tool": "Tool-Call testen",
        "test.prompt": "Prompt:",
        "test.output": "Antwort (Rohdaten von POST {path}):",
        "test.default_prompt": "Sag einfach: Hallo",
        "test.tool_prompt": "Wie ist das Wetter in Berlin? Nutze das Tool.",
        "test.weather_desc": "Liefert das aktuelle Wetter für eine Stadt.",
        "test.kind_tool": "Tool-Test",
        "test.kind_prompt": "Prompt",
        "test.running": "{label} läuft …",
        "test.error": "{label}: Fehler nach {s:.1f}s",
        "test.done": "{label}: fertig in {s:.1f}s{verdict}",
        "test.tool_ok": " – ✔ Tool-Call korrekt erkannt",
        "test.tool_missing": " – ✘ kein Tool-Call in der Antwort",
        "dlg.invalid": "Ungültiger Wert",
        "dlg.int": "„{label}“ muss eine ganze Zahl sein.",
        "dlg.ports": "„{label}“: Portnummern mit Komma trennen, z. B. 1234, 11434",
        "dlg.key_short_title": "API-Key zu kurz",
        "dlg.key_short": "Jeder API-Key braucht mindestens 16 Zeichen. Am einfachsten „Generieren“ klicken.",
        "dlg.net_nokey_title": "Netzwerk ohne API-Key",
        "dlg.net_nokey": "Mit Host {host} ist der Proxy im Netzwerk erreichbar. Ohne API-Key kann "
                         "jeder dort dein Claude-Abo benutzen.\n\nJetzt einen sicheren API-Key erzeugen?\n"
                         "(Ja = Key erzeugen · Nein = trotzdem ohne Key speichern)",
        "dlg.restart_title": "Neustart nötig",
        "dlg.restart": "Die Änderungen gelten erst nach einem Neustart. Jetzt neu starten?",
        "dlg.newkey_title": "Neuer API-Key",
        "dlg.newkey": "Den bestehenden Key ersetzen? Clients mit dem alten Key funktionieren danach "
                      "nicht mehr.",
        "dlg.nokey_title": "Kein API-Key",
        "dlg.nokey": "Es ist kein API-Key eingetragen.",
        "dlg.port_busy_title": "Port belegt",
        "dlg.port_busy": "Auf {url} läuft bereits ein Proxy (z. B. über start.bat). Schließe ihn "
                         "zuerst, damit die UI ihn starten und steuern kann.",
        "dlg.start_failed": "Start fehlgeschlagen",
        "dlg.not_reachable_title": "Proxy nicht erreichbar",
        "dlg.not_reachable": "Starte zuerst den Proxy.",
        "dlg.quit_title": "Beenden",
        "dlg.quit": "Der Proxy wird beim Schließen beendet. Fortfahren?",
        "dlg.unsaved": "Es gibt nicht gespeicherte Einstellungen. Trotzdem schließen?",
        "log.started": "[UI] Proxy gestartet (PID {pid})",
        "log.exited": "[UI] Proxy beendet (Exit-Code {code})",
        "log.saved": "[UI] Einstellungen gespeichert → {path}",
        "log.copied": "[UI] In Zwischenablage kopiert: {text}",
        "log.key_generated": "[UI] Neuer API-Key erzeugt – zum Übernehmen „Speichern“ klicken.",
        "log.key_copied": "[UI] API-Key in die Zwischenablage kopiert.",
        "det.title": "Anfrage {id} – was der Proxy gemacht hat",
        "det.loading": "Lade …",
        "det.flow": "App ─①─▶ Proxy ─②+③─▶ claude --print ─④─▶ Proxy ─⑤─▶ App",
        "det.tab.overview": "Überblick",
        "det.tab.client_request": "① Client-Anfrage",
        "det.tab.system_prompt": "② System-Prompt",
        "det.tab.prompt": "③ Prompt",
        "det.tab.raw_output": "④ Rohantwort",
        "det.tab.result": "⑤ Ergebnis",
        "det.help.client_request": "So kam die Anfrage beim Proxy an – im Format der App "
                                   "(OpenAI, Anthropic, Ollama …).",
        "det.help.system_prompt": "System-Prompt, den der Proxy an die CLI übergibt: Systemanweisungen der "
                                  "App + Tool-Definitionen mit dem <proxy_tool_call>-Protokoll "
                                  "(+ ggf. Quota-Warnung oder JSON-Anweisung).",
        "det.help.prompt": "Die CLI nimmt nur EINE Nachricht an. Deshalb fasst der Proxy den ganzen "
                           "Verlauf zu einem Text zusammen: frühere Nachrichten als Transkript, die "
                           "aktuelle Nachricht hervorgehoben.",
        "det.help.raw_output": "Was Claude geantwortet hat, bevor der Proxy etwas geändert hat. "
                               "Tool-Aufrufe stehen hier noch als <proxy_tool_call>-Text.",
        "det.help.result": "Nach der Umwandlung: Tool-Tags sind zu echten Tool-Calls geworden. Daraus "
                           "baut der Proxy die Antwort im Format der App.",
        "det.none": "(nicht verfügbar – Details werden nur für die letzten 50 Anfragen gespeichert)",
        "det.fetch_error": "Details konnten nicht geladen werden: {err}",
        "det.cli": "CLI-Aufruf",
        "det.error": "Fehler",
        "det.path": "Pfad",
    },
    "en": {
        "status.stopped": "Stopped",
        "status.running": "Running",
        "status.starting": "Starting …",
        "status.external": "Running (started elsewhere)",
        "status.crashed": "Crashed (code {code}) – see log",
        "status.key_changed": "Running – API key changed, please restart",
        "btn.start": "▶ Start",
        "btn.stop": "■ Stop",
        "btn.restart": "⟳ Restart",
        "btn.copy": "Copy",
        "header.base_url": "Base URL (OpenAI):",
        "header.language": "Language:",
        "lang.auto": "Automatic ({name})",
        "badge.local": "This PC only",
        "badge.net_key": "🔒 Network · API key required",
        "badge.net_open": "⚠ Network · NO API key",
        "tab.settings": "Settings",
        "tab.requests": "Requests",
        "tab.log": "Log",
        "tab.quota": "Quota",
        "tab.test": "Test",
        "tab.howto": "How it works",
        "settings.section": "Proxy",
        "f.PROXY_HOST": "Host",
        "h.PROXY_HOST": "127.0.0.1 = this PC only. 0.0.0.0 = reachable on your local network "
                        "(set an API key then!).",
        "f.PROXY_PORT": "Port",
        "h.PROXY_PORT": "Port the proxy listens on.",
        "f.PROXY_API_KEY": "API key",
        "h.PROXY_API_KEY": "Empty = no protection. Set = clients must send it as their API key. "
                           "Separate several keys with commas.",
        "f.PROXY_AUTH_EXEMPT_LOCALHOST": "No key from this PC",
        "h.PROXY_AUTH_EXEMPT_LOCALHOST": "Requests from this PC itself need no key – only network "
                                         "clients do.",
        "f.PROXY_EXTRA_PORTS": "Extra ports",
        "h.PROXY_EXTRA_PORTS": "Optional, comma-separated. E.g. 1234 (LM Studio) and 11434 (Ollama) "
                               "for clients that expect those ports. Ports in use are skipped.",
        "f.PROXY_DEFAULT_MODEL": "Default model",
        "h.PROXY_DEFAULT_MODEL": "Used when a client sends no (or an unknown) model. Alias "
                                 "(sonnet/opus/haiku) or a full model ID.",
        "f.CLAUDE_BIN": "Claude CLI",
        "h.CLAUDE_BIN": "Name or full path of the claude CLI (must be logged in).",
        "f.PROXY_SUBPROCESS_TIMEOUT": "Timeout (seconds)",
        "h.PROXY_SUBPROCESS_TIMEOUT": "Maximum run time of a single request.",
        "f.PROXY_LOG_LEVEL": "Log level",
        "h.PROXY_LOG_LEVEL": "DEBUG shows the most detail.",
        "f.PROXY_QUOTA_WARN_RESET_MIN": "Quota warning: minutes before reset",
        "h.PROXY_QUOTA_WARN_RESET_MIN": "This many minutes before the usage window resets, the model "
                                        "is told to wrap up.",
        "f.PROXY_QUOTA_HEAVY_TURNS": "Quota warning: after turns",
        "h.PROXY_QUOTA_HEAVY_TURNS": "Soft warning after this many requests in the 5-hour window.",
        "f.PROXY_STATE_DIR": "State folder",
        "h.PROXY_STATE_DIR": "Holds quota.json and the CLI's empty working folder.",
        "key.show": "Show",
        "key.hide": "Hide",
        "key.generate": "Generate",
        "settings.autostart": "Start the proxy automatically when the UI opens",
        "btn.save": "Save",
        "btn.save_restart": "Save & restart",
        "btn.defaults": "Defaults",
        "settings.saved_in": "Saved in: {path}",
        "settings.dirty": "● Unsaved changes – they take effect only after \"Save\"",
        "settings.clean": "✓ All saved",
        "howto.section": "Client setup – base URL per format",
        "howto.openai": "OpenAI-compatible (stio, Continue, Open WebUI …)",
        "howto.lmstudio": "LM Studio clients",
        "howto.responses": "OpenAI Responses API (Codex …)",
        "howto.anthropic": "Anthropic (Claude SDKs, Claude Code …)",
        "howto.no_v1": "(without /v1)",
        "howto.ollama": "Ollama clients",
        "howto.remote": "From other devices (+ path as above)",
        "howto.key_set": "API key:  enter the key from \"API key\" above "
                         "(sent as bearer token or x-api-key)",
        "howto.key_local": "          From this PC it also works without a key.",
        "howto.key_none": "API key:  anything (e.g. x) – ignored",
        "howto.model": "Model:    {model}  (or {others}); unknown names like gpt-4o → default model",
        "howto.extra": "Extra ports: {ports} – every format is served there too",
        "col.time": "Time",
        "col.api": "Format",
        "col.model": "Model",
        "col.stream": "Stream",
        "col.msgs": "Msgs",
        "col.tools": "Tools",
        "col.tool_calls": "Tool calls",
        "col.duration": "Duration",
        "col.status": "Status",
        "col.preview": "Last message",
        "requests.legend": "Yellow = running · Blue = tool call detected · Red = error · Double-click "
                           "shows what the proxy sent to Claude. The list lives in the proxy and is "
                           "cleared on restart.",
        "yes": "yes",
        "no": "no",
        "running": "running",
        "log.autoscroll": "Auto-scroll",
        "log.clear": "Clear",
        "quota.section": "Subscription usage",
        "q.status": "Status",
        "q.rate_limit_type": "Limit type",
        "q.resets_at": "Window resets",
        "q.session_turns": "Requests in window",
        "q.session_cost_usd": "API equivalent (USD)",
        "q.session_started_at": "Window since",
        "q.is_using_overage": "Using overage",
        "q.last_event_at": "Last limit event",
        "quota.note": "The USD value is only the API-price equivalent for reference – your "
                      "subscription doesn't charge it.",
        "quota.in_min": "in {m} min",
        "quota.warn_from": "warning from {n}",
        "test.format": "Format:",
        "test.model": "Model:",
        "test.send": "Send prompt",
        "test.tool": "Test tool call",
        "test.prompt": "Prompt:",
        "test.output": "Response (raw data from POST {path}):",
        "test.default_prompt": "Just say: Hello",
        "test.tool_prompt": "What's the weather in Berlin? Use the tool.",
        "test.weather_desc": "Returns the current weather for a city.",
        "test.kind_tool": "Tool test",
        "test.kind_prompt": "Prompt",
        "test.running": "{label} running …",
        "test.error": "{label}: error after {s:.1f}s",
        "test.done": "{label}: done in {s:.1f}s{verdict}",
        "test.tool_ok": " – ✔ tool call detected correctly",
        "test.tool_missing": " – ✘ no tool call in the response",
        "dlg.invalid": "Invalid value",
        "dlg.int": "\"{label}\" must be a whole number.",
        "dlg.ports": "\"{label}\": separate port numbers with commas, e.g. 1234, 11434",
        "dlg.key_short_title": "API key too short",
        "dlg.key_short": "Every API key needs at least 16 characters. Easiest: click \"Generate\".",
        "dlg.net_nokey_title": "Network without API key",
        "dlg.net_nokey": "With host {host} the proxy is reachable on your network. Without an API "
                         "key anyone there can use your Claude subscription.\n\nGenerate a secure "
                         "API key now?\n(Yes = generate key · No = save without a key anyway)",
        "dlg.restart_title": "Restart needed",
        "dlg.restart": "Changes take effect after a restart. Restart now?",
        "dlg.newkey_title": "New API key",
        "dlg.newkey": "Replace the existing key? Clients using the old key will stop working.",
        "dlg.nokey_title": "No API key",
        "dlg.nokey": "No API key has been entered.",
        "dlg.port_busy_title": "Port in use",
        "dlg.port_busy": "A proxy is already running at {url} (e.g. via start.bat). Close it first "
                         "so the UI can start and control it.",
        "dlg.start_failed": "Start failed",
        "dlg.not_reachable_title": "Proxy not reachable",
        "dlg.not_reachable": "Start the proxy first.",
        "dlg.quit_title": "Quit",
        "dlg.quit": "The proxy will be stopped when you close this window. Continue?",
        "dlg.unsaved": "There are unsaved settings. Close anyway?",
        "log.started": "[UI] Proxy started (PID {pid})",
        "log.exited": "[UI] Proxy exited (exit code {code})",
        "log.saved": "[UI] Settings saved → {path}",
        "log.copied": "[UI] Copied to clipboard: {text}",
        "log.key_generated": "[UI] New API key generated – click \"Save\" to apply it.",
        "log.key_copied": "[UI] API key copied to clipboard.",
        "det.title": "Request {id} – what the proxy did",
        "det.loading": "Loading …",
        "det.flow": "App ─①─▶ Proxy ─②+③─▶ claude --print ─④─▶ Proxy ─⑤─▶ App",
        "det.tab.overview": "Overview",
        "det.tab.client_request": "① Client request",
        "det.tab.system_prompt": "② System prompt",
        "det.tab.prompt": "③ Prompt",
        "det.tab.raw_output": "④ Raw output",
        "det.tab.result": "⑤ Result",
        "det.help.client_request": "The request as it reached the proxy – in the app's format "
                                   "(OpenAI, Anthropic, Ollama …).",
        "det.help.system_prompt": "System prompt the proxy passes to the CLI: the app's system "
                                  "instructions + tool definitions with the <proxy_tool_call> protocol "
                                  "(+ a quota warning or JSON instruction, if any).",
        "det.help.prompt": "The CLI accepts only ONE message, so the proxy flattens the whole "
                           "conversation into one text: earlier turns as a transcript, the current "
                           "message highlighted.",
        "det.help.raw_output": "What Claude replied before the proxy changed anything. Tool calls "
                               "are still <proxy_tool_call> text here.",
        "det.help.result": "After conversion: tool tags have become real tool calls. From this the "
                           "proxy builds the reply in the app's format.",
        "det.none": "(not available – details are kept for the last 50 requests only)",
        "det.fetch_error": "Could not load details: {err}",
        "det.cli": "CLI call",
        "det.error": "Error",
        "det.path": "Path",
    },
}

# "How it works" tab: (style, text) segments. Styles: h = heading, p = body,
# m = monospace diagram, s = numbered step, b = bullet.
HOWTO: dict[str, list[tuple[str, str]]] = {
    "de": [
        ("h", "Was der Proxy macht"),
        ("p", "Der Proxy ist ein kleiner Webserver auf deinem PC. Er nimmt API-Anfragen im Format "
              "anderer Anbieter entgegen (OpenAI, Anthropic, Ollama, LM Studio) und beantwortet sie "
              "mit deinem Claude-Abo – über die offizielle claude-CLI statt über einen "
              "kostenpflichtigen API-Key."),
        ("m", "  Deine App ──HTTP──▶  Proxy  ──startet──▶  claude --print  ──▶  Anthropic\n"
              "  (stio, …)           (dieser PC)            (dein Login)          (dein Abo)"),
        ("h", "Ablauf einer Anfrage"),
        ("s", "①  Anfrage annehmen – Die App schickt eine Anfrage in ihrem Format. Ist ein API-Key "
              "gesetzt, prüft der Proxy ihn zuerst."),
        ("s", "②  System-Prompt bauen – Systemanweisungen der App plus alle Tool-Definitionen. Weil "
              "die CLI keine fremden Tools annehmen kann, beschreibt der Proxy sie als Text und bittet "
              "Claude, Aufrufe als <proxy_tool_call>{…}</proxy_tool_call> zu schreiben."),
        ("s", "③  Prompt bauen – Die CLI nimmt nur eine einzige Nachricht an. Deshalb wird der ganze "
              "Chatverlauf zu einem Text zusammengefasst: frühere Nachrichten als Transkript, "
              "Tool-Ergebnisse als <proxy_tool_result>, die aktuelle Nachricht hervorgehoben."),
        ("s", "④  Claude ausführen – Für jede Anfrage startet der Proxy einen neuen Prozess "
              "„claude --print“: isoliert (ohne eigene Tools, MCP-Server oder CLAUDE.md) und in einem "
              "leeren Ordner. Angemeldet ist er über deinen normalen Claude-Login."),
        ("s", "⑤  Antwort umwandeln – Text wird, wenn möglich, live weitergestreamt. "
              "<proxy_tool_call>-Tags werden in echte Tool-Calls im Format der App umgewandelt, und die "
              "Antwort geht so zurück, wie die App sie erwartet."),
        ("h", "Was das bedeutet"),
        ("b", "Tools laufen über den Prompt, nicht nativ. Das klappt meist zuverlässig, ist aber nicht "
              "garantiert – vor allem kleinere Modelle (haiku) weichen manchmal vom Format ab."),
        ("b", "Jede Anfrage startet die CLI neu (einige Sekunden Overhead) und schickt den kompletten "
              "Verlauf noch einmal mit."),
        ("b", "Mit Tools oder JSON-Modus wartet der Proxy die ganze Antwort ab und liefert sie dann "
              "am Stück aus."),
        ("b", "Bilder und Dateien werden nicht übertragen; Embeddings gibt es nicht."),
        ("b", "Alles zählt auf das Kontingent deines Abos (Tab „Quota“)."),
        ("h", "Selbst nachsehen"),
        ("p", "Im Tab „Anfragen“ eine Zeile doppelklicken. Du siehst dann genau, was die App geschickt "
              "hat (①), welchen System-Prompt (②) und Prompt (③) der Proxy daraus gemacht hat, was "
              "Claude roh geantwortet hat (④) und was daraus für die App wurde (⑤)."),
    ],
    "en": [
        ("h", "What the proxy does"),
        ("p", "The proxy is a small web server on your PC. It accepts API requests in other providers' "
              "formats (OpenAI, Anthropic, Ollama, LM Studio) and answers them with your Claude "
              "subscription – through the official claude CLI instead of a paid API key."),
        ("m", "  Your app ──HTTP──▶  Proxy  ──starts──▶  claude --print  ──▶  Anthropic\n"
              "  (stio, …)          (this PC)            (your login)         (your plan)"),
        ("h", "What happens to a request"),
        ("s", "①  Accept the request – The app sends a request in its own format. If an API key is "
              "set, the proxy checks it first."),
        ("s", "②  Build the system prompt – The app's system instructions plus every tool "
              "definition. The CLI can't take foreign tools, so the proxy describes them as text and "
              "asks Claude to write calls as <proxy_tool_call>{…}</proxy_tool_call>."),
        ("s", "③  Build the prompt – The CLI accepts a single message only, so the whole "
              "conversation is flattened into one text: earlier turns as a transcript, tool results as "
              "<proxy_tool_result>, the current message highlighted."),
        ("s", "④  Run Claude – For every request the proxy starts a fresh \"claude --print\" process: "
              "isolated (no own tools, MCP servers or CLAUDE.md) in an empty folder, signed in through "
              "your normal Claude login."),
        ("s", "⑤  Convert the reply – Text is streamed through live where possible. "
              "<proxy_tool_call> tags become real tool calls in the app's format, and the reply goes "
              "back exactly the way the app expects it."),
        ("h", "What that means"),
        ("b", "Tools work through the prompt, not natively. That is usually reliable but not "
              "guaranteed – smaller models (haiku) especially drift from the format now and then."),
        ("b", "Every request restarts the CLI (a few seconds of overhead) and resends the whole "
              "conversation."),
        ("b", "With tools or JSON mode the proxy waits for the complete reply and delivers it in one "
              "piece."),
        ("b", "Images and files are not passed on; there are no embeddings."),
        ("b", "Everything counts against your subscription's usage (\"Quota\" tab)."),
        ("h", "See for yourself"),
        ("p", "Double-click a row in the \"Requests\" tab. You'll see exactly what the app sent (①), "
              "which system prompt (②) and prompt (③) the proxy built from it, what Claude replied "
              "raw (④) and what the app got back (⑤)."),
    ],
}

_lang = "en"


def t(key: str, **kwargs) -> str:
    text = TEXTS[_lang].get(key) or TEXTS["en"].get(key) or key
    return text.format(**kwargs) if kwargs else text


def system_language() -> str:
    """'de' if the OS UI language is German, else 'en'."""
    if os.name == "nt":
        try:
            import ctypes
            lang_id = ctypes.windll.kernel32.GetUserDefaultUILanguage()
            return "de" if (lang_id & 0x3FF) == 0x07 else "en"  # LANG_GERMAN
        except Exception:  # noqa: BLE001
            pass
    candidates = [os.environ.get(v) for v in ("LC_ALL", "LC_MESSAGES", "LANG")]
    try:
        candidates.append(locale.getlocale()[0])
    except ValueError:
        pass
    for value in candidates:
        if value:
            return "de" if value.lower().startswith(("de", "german")) else "en"
    return "en"


def resolve_language(setting: str | None) -> str:
    return setting if setting in LANG_NAMES else system_language()


# ---------------------------------------------------------------- helpers ---

TOOL_TEST_NAME = "get_weather"
_WEATHER_SCHEMA = {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}


def _weather_tool() -> dict:
    return {"name": TOOL_TEST_NAME, "description": t("test.weather_desc"), "parameters": _WEATHER_SCHEMA}


def _has_tool_call(fmt: str, result: dict) -> bool:
    if fmt == "OpenAI Chat":
        return bool(((result.get("choices") or [{}])[0].get("message") or {}).get("tool_calls"))
    if fmt == "OpenAI Responses":
        return any(o.get("type") == "function_call" for o in result.get("output") or [])
    if fmt == "Anthropic":
        return result.get("stop_reason") == "tool_use"
    if fmt == "Ollama":
        return bool((result.get("message") or {}).get("tool_calls"))
    return False


# Test console formats: name -> (path, build(prompt, model, with_tool) -> payload)
TEST_FORMATS = {
    "OpenAI Chat": ("/v1/chat/completions", lambda p, m, tool: {
        "model": m, "messages": [{"role": "user", "content": p}],
        **({"tools": [{"type": "function", "function": _weather_tool()}]} if tool else {})}),
    "OpenAI Responses": ("/v1/responses", lambda p, m, tool: {
        "model": m, "input": p,
        **({"tools": [{"type": "function", **_weather_tool()}]} if tool else {})}),
    "Anthropic": ("/v1/messages", lambda p, m, tool: {
        "model": m, "max_tokens": 1024, "messages": [{"role": "user", "content": p}],
        **({"tools": [{"name": TOOL_TEST_NAME, "description": t("test.weather_desc"),
                       "input_schema": _WEATHER_SCHEMA}]} if tool else {})}),
    "Ollama": ("/api/chat", lambda p, m, tool: {
        "model": m, "stream": False, "messages": [{"role": "user", "content": p}],
        **({"tools": [{"type": "function", "function": _weather_tool()}]} if tool else {})}),
    "OpenAI Legacy": ("/v1/completions", lambda p, m, tool: {"model": m, "prompt": p}),
}


def read_config_file() -> dict:
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def load_config() -> dict:
    cfg = dict(DEFAULTS)
    cfg["UI_AUTOSTART"] = False
    cfg["UI_LANGUAGE"] = "auto"
    cfg.update(read_config_file())
    return cfg


def save_config(cfg: dict) -> None:
    tmp = CONFIG_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
    os.replace(tmp, CONFIG_FILE)


def proxy_python() -> str:
    """Prefer the project's venv (it has aiohttp); fall back to this interpreter."""
    candidates = [
        os.path.join(HERE, ".venv", "Scripts", "python.exe"),
        os.path.join(HERE, "venv", "Scripts", "python.exe"),
        os.path.join(HERE, ".venv", "bin", "python"),
        os.path.join(HERE, "venv", "bin", "python"),
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
    exe = sys.executable
    # pythonw has no stdout; the proxy needs a console interpreter for its log.
    if exe.lower().endswith("pythonw.exe"):
        exe = exe[:-len("pythonw.exe")] + "python.exe"
    return exe


def local_base(cfg: dict) -> str:
    host = str(cfg.get("PROXY_HOST") or "127.0.0.1")
    if host in ("0.0.0.0", "::", ""):
        host = "127.0.0.1"
    return f"http://{host}:{cfg.get('PROXY_PORT') or 3456}"


def network_bases(cfg: dict) -> list[str]:
    """Base URLs other devices on the LAN would use, if the proxy is exposed."""
    host = str(cfg.get("PROXY_HOST") or "127.0.0.1")
    if host in LOOPBACK_HOSTS:
        return []
    port = cfg.get("PROXY_PORT") or 3456
    if host not in ("0.0.0.0", "::", ""):
        return [f"http://{host}:{port}"]
    ips: set[str] = set()
    try:  # UDP "connect" sends nothing; it just picks the outgoing interface
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 80))
            ips.add(s.getsockname()[0])
    except OSError:
        pass
    try:
        ips.update(info[4][0] for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET))
    except OSError:
        pass
    return [f"http://{ip}:{port}" for ip in sorted(ips) if not ip.startswith(("127.", "169.254."))]


def split_keys(value: str) -> list[str]:
    return [k for k in re.split(r"[,\s]+", value or "") if k]


def first_key(cfg: dict) -> str:
    return next(iter(split_keys(str(cfg.get("PROXY_API_KEY") or ""))), "")


def new_api_key() -> str:
    return "sk-proxy-" + secrets.token_urlsafe(32)


def is_true(value) -> bool:
    return str(value).lower() in ("1", "true", "yes", "on")


def http_json(url: str, payload: dict | None = None, timeout: float = 3.0, api_key: str = ""):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fmt_ts(ts: float | int | None, with_date: bool = False) -> str:
    if not ts:
        return "–"
    fmt = ("%d.%m. %H:%M:%S" if _lang == "de" else "%m/%d %H:%M:%S") if with_date else "%H:%M:%S"
    return time.strftime(fmt, time.localtime(float(ts)))


# ---------------------------------------------------------- proxy process ---

class ProxyProcess:
    """Runs proxy.py as a child process and pumps its output into a queue."""

    def __init__(self, out: queue.Queue):
        self.out = out
        self.proc: subprocess.Popen | None = None

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    @property
    def exit_code(self) -> int | None:
        return None if self.proc is None else self.proc.poll()

    def start(self) -> None:
        if self.running:
            return
        env = dict(os.environ)
        # config.json is the single source of truth when started from the UI.
        for key in FIELD_KEYS:
            env.pop(key, None)
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        self.proc = subprocess.Popen(
            [proxy_python(), "-u", PROXY_SCRIPT],
            cwd=HERE, env=env,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            creationflags=flags,
        )
        threading.Thread(target=self._pump, args=(self.proc,), daemon=True).start()
        self.out.put(t("log.started", pid=self.proc.pid))

    def _pump(self, proc: subprocess.Popen) -> None:
        assert proc.stdout
        for raw in proc.stdout:
            self.out.put(raw.decode("utf-8", errors="replace").rstrip("\r\n"))
        self.out.put(t("log.exited", code=proc.wait()))

    def stop(self) -> None:
        if not self.running:
            return
        assert self.proc
        self.proc.terminate()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()


# --------------------------------------------------------------------- UI ---

class App(tk.Tk):
    POLL_MS = 400
    FETCH_EVERY_S = 2.0
    TAB_LOG = 2

    def __init__(self) -> None:
        global _lang
        super().__init__()
        self.title("Claude Subscription Proxy")
        self.geometry("1100x800")
        self.minsize(860, 560)

        self.cfg = load_config()
        _lang = resolve_language(self.cfg.get("UI_LANGUAGE"))
        self.log_queue: queue.Queue = queue.Queue()
        self.result_queue: queue.Queue = queue.Queue()
        self.proxy = ProxyProcess(self.log_queue)
        self.health_ok = False
        self.auth_mismatch = False
        self._fetching = False
        self._last_fetch = 0.0
        self._requests: dict[str, dict] = {}

        self._build_style()
        self._build_ui()

        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(self.POLL_MS, self.poll)
        if self.cfg.get("UI_AUTOSTART"):
            self.after(300, self.start_proxy)

    # ---- layout -----------------------------------------------------------

    def _build_style(self) -> None:
        style = ttk.Style(self)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("TNotebook.Tab", padding=(14, 5))
        style.configure("Status.TLabel", font=("Segoe UI", 11, "bold"))
        style.configure("Help.TLabel", foreground="#666666")
        style.configure("Dirty.TLabel", foreground="#b35900", font=("Segoe UI", 9, "bold"))
        style.configure("Clean.TLabel", foreground="#1a7f37")
        style.configure("Section.TLabelframe.Label", font=("Segoe UI", 10, "bold"))

    def _build_ui(self) -> None:
        self._build_header()
        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        self._build_settings_tab()
        self._build_requests_tab()
        self._build_log_tab()
        self._build_quota_tab()
        self._build_test_tab()
        self._build_howto_tab()

    def _build_header(self) -> None:
        bar = ttk.Frame(self, padding=(10, 10, 10, 6))
        bar.pack(fill="x")

        self.status_dot = tk.Canvas(bar, width=16, height=16, highlightthickness=0)
        self.status_dot.pack(side="left")
        self._dot = self.status_dot.create_oval(2, 2, 14, 14, fill="#999999", outline="")
        self.status_label = ttk.Label(bar, text=t("status.stopped"), style="Status.TLabel")
        self.status_label.pack(side="left", padx=(6, 10))
        self.access_label = tk.Label(bar, font=("Segoe UI", 9, "bold"), padx=8, pady=2)
        self.access_label.pack(side="left", padx=(0, 14))

        self.btn_start = ttk.Button(bar, text=t("btn.start"), command=self.start_proxy)
        self.btn_stop = ttk.Button(bar, text=t("btn.stop"), command=self.stop_proxy)
        self.btn_restart = ttk.Button(bar, text=t("btn.restart"), command=self.restart_proxy)
        for b in (self.btn_start, self.btn_stop, self.btn_restart):
            b.pack(side="left", padx=2)

        # Right side, packed right-to-left: language, then base URL.
        self._lang_codes = ["auto", "de", "en"]
        lang_values = [t("lang.auto", name=LANG_NAMES[system_language()]), LANG_NAMES["de"],
                       LANG_NAMES["en"]]
        current = self.cfg.get("UI_LANGUAGE") if self.cfg.get("UI_LANGUAGE") in LANG_NAMES else "auto"
        self.lang_var = tk.StringVar(value=lang_values[self._lang_codes.index(current)])
        lang_box = ttk.Combobox(bar, textvariable=self.lang_var, values=lang_values,
                                width=23, state="readonly")
        lang_box.pack(side="right")
        lang_box.bind("<<ComboboxSelected>>",
                      lambda _e: self.set_language(self._lang_codes[lang_box.current()]))
        ttk.Label(bar, text=t("header.language")).pack(side="right", padx=(16, 4))

        ttk.Button(bar, text=t("btn.copy"), command=self.copy_base_url).pack(side="right")
        self.base_url_var = tk.StringVar()
        ttk.Entry(bar, textvariable=self.base_url_var, width=28, state="readonly").pack(side="right", padx=4)
        ttk.Label(bar, text=t("header.base_url")).pack(side="right")
        self._refresh_base_url()
        self._refresh_access_badge()

    def _build_settings_tab(self) -> None:
        tab = ttk.Frame(self.notebook, padding=12)
        self.notebook.add(tab, text=t("tab.settings"))

        form = ttk.LabelFrame(tab, text=t("settings.section"), padding=10, style="Section.TLabelframe")
        form.pack(fill="x")
        form.columnconfigure(2, weight=1)
        self.field_vars: dict[str, tk.StringVar] = {}
        for row, (key, default, kind) in enumerate(FIELDS):
            ttk.Label(form, text=t(f"f.{key}")).grid(row=row, column=0, sticky="w", pady=3, padx=(0, 10))
            value = str(self.cfg.get(key, default))
            if kind == "bool":
                value = "true" if is_true(value) else "false"
            var = tk.StringVar(value=value)
            self.field_vars[key] = var
            if kind == "model":
                widget = ttk.Combobox(form, textvariable=var, values=MODELS, width=30)
            elif kind.startswith("choice:"):
                widget = ttk.Combobox(form, textvariable=var, values=kind[7:].split(","),
                                      width=30, state="readonly")
            elif kind == "host":
                widget = ttk.Combobox(form, textvariable=var, values=["127.0.0.1", "0.0.0.0"], width=30)
            elif kind == "bool":
                widget = ttk.Checkbutton(form, variable=var, onvalue="true", offvalue="false")
            elif kind == "secret":
                widget = self.key_entry = ttk.Entry(form, textvariable=var, width=33, show="•")
            else:
                widget = ttk.Entry(form, textvariable=var, width=33)
            widget.grid(row=row, column=1, sticky="w", pady=3)

            help_cell = ttk.Frame(form)
            help_cell.grid(row=row, column=2, sticky="w", padx=(12, 0))
            if kind == "secret":
                self.key_show_btn = ttk.Button(help_cell, text=t("key.show"), width=10,
                                               command=self.toggle_key_visible)
                self.key_show_btn.pack(side="left")
                ttk.Button(help_cell, text=t("key.generate"), width=11,
                           command=self.generate_key).pack(side="left", padx=2)
                ttk.Button(help_cell, text=t("btn.copy"), width=9,
                           command=self.copy_key).pack(side="left", padx=(0, 8))
            ttk.Label(help_cell, text=t(f"h.{key}"), style="Help.TLabel",
                      wraplength=320 if kind == "secret" else 560).pack(side="left")
            var.trace_add("write", lambda *_: self._refresh_dirty())

        self.autostart_var = tk.BooleanVar(value=bool(self.cfg.get("UI_AUTOSTART")))
        self.autostart_var.trace_add("write", lambda *_: self._refresh_dirty())
        ttk.Checkbutton(form, text=t("settings.autostart"), variable=self.autostart_var).grid(
            row=len(FIELDS), column=0, columnspan=3, sticky="w", pady=(8, 0))

        buttons = ttk.Frame(tab, padding=(0, 10))
        buttons.pack(fill="x")
        ttk.Button(buttons, text=t("btn.save"), command=self.save_settings).pack(side="left")
        ttk.Button(buttons, text=t("btn.save_restart"),
                   command=lambda: self.save_settings(restart=True)).pack(side="left", padx=6)
        ttk.Button(buttons, text=t("btn.defaults"), command=self.reset_defaults).pack(side="left")
        self.dirty_label = ttk.Label(buttons)
        self.dirty_label.pack(side="left", padx=14)
        ttk.Label(buttons, text=t("settings.saved_in", path=CONFIG_FILE),
                  style="Help.TLabel").pack(side="right")

        howto = ttk.LabelFrame(tab, text=t("howto.section"), padding=10, style="Section.TLabelframe")
        howto.pack(fill="x", pady=(6, 0))
        self.howto_var = tk.StringVar()
        ttk.Label(howto, textvariable=self.howto_var, justify="left",
                  font=("Consolas", 10)).pack(anchor="w")
        self._refresh_howto()
        self._refresh_dirty()

    def _build_requests_tab(self) -> None:
        tab = ttk.Frame(self.notebook, padding=8)
        self.notebook.add(tab, text=t("tab.requests"))
        cols = [("time", 70), ("api", 100), ("model", 90), ("stream", 55), ("msgs", 45),
                ("tools", 45), ("tool_calls", 170), ("duration", 65), ("status", 60), ("preview", 380)]
        frame = ttk.Frame(tab)
        frame.pack(fill="both", expand=True)
        self.req_tree = ttk.Treeview(frame, columns=[c[0] for c in cols], show="headings")
        for key, width in cols:
            self.req_tree.heading(key, text=t(f"col.{key}"))
            self.req_tree.column(key, width=width, stretch=(key == "preview"),
                                 anchor="w" if key in ("preview", "tool_calls", "model") else "center")
        self.req_tree.tag_configure("error", background="#fde2e2")
        self.req_tree.tag_configure("running", background="#fff6d6")
        self.req_tree.tag_configure("tool", background="#e3f1ff")
        sb = ttk.Scrollbar(frame, orient="vertical", command=self.req_tree.yview)
        self.req_tree.configure(yscrollcommand=sb.set)
        self.req_tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.req_tree.bind("<Double-1>", self.show_request_details)
        ttk.Label(tab, text=t("requests.legend"), style="Help.TLabel").pack(anchor="w", pady=(6, 0))

    def _build_log_tab(self) -> None:
        tab = ttk.Frame(self.notebook, padding=8)
        self.notebook.add(tab, text=t("tab.log"))
        top = ttk.Frame(tab)
        top.pack(fill="x", pady=(0, 6))
        self.autoscroll_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(top, text=t("log.autoscroll"), variable=self.autoscroll_var).pack(side="left")
        ttk.Button(top, text=t("log.clear"),
                   command=lambda: self.log_text.delete("1.0", "end")).pack(side="right")
        self.log_text = ScrolledText(tab, font=("Consolas", 9), wrap="none",
                                     background="#1e1e1e", foreground="#d4d4d4",
                                     insertbackground="#d4d4d4")
        self.log_text.pack(fill="both", expand=True)
        self.log_text.tag_configure("warn", foreground="#e5c07b")
        self.log_text.tag_configure("error", foreground="#f47171")
        self.log_text.tag_configure("ui", foreground="#61afef")
        self.log_text.tag_configure("tool", foreground="#98c379")

    def _build_quota_tab(self) -> None:
        tab = ttk.Frame(self.notebook, padding=12)
        self.notebook.add(tab, text=t("tab.quota"))
        self.quota_vars: dict[str, tk.StringVar] = {}
        rows = ["status", "rate_limit_type", "resets_at", "session_turns", "session_cost_usd",
                "session_started_at", "is_using_overage", "last_event_at"]
        grid = ttk.LabelFrame(tab, text=t("quota.section"), padding=10, style="Section.TLabelframe")
        grid.pack(fill="x")
        for i, key in enumerate(rows):
            ttk.Label(grid, text=t(f"q.{key}") + ":").grid(row=i, column=0, sticky="w", pady=2, padx=(0, 16))
            var = tk.StringVar(value="–")
            self.quota_vars[key] = var
            ttk.Label(grid, textvariable=var, font=("Segoe UI", 10, "bold")).grid(row=i, column=1, sticky="w")
        ttk.Label(tab, text=t("quota.note"), style="Help.TLabel").pack(anchor="w", pady=(8, 0))

    def _build_test_tab(self) -> None:
        tab = ttk.Frame(self.notebook, padding=12)
        self.notebook.add(tab, text=t("tab.test"))
        top = ttk.Frame(tab)
        top.pack(fill="x")
        ttk.Label(top, text=t("test.format")).pack(side="left")
        self.test_format_var = tk.StringVar(value="OpenAI Chat")
        fmt_box = ttk.Combobox(top, textvariable=self.test_format_var, values=list(TEST_FORMATS),
                               width=18, state="readonly")
        fmt_box.pack(side="left", padx=(6, 14))
        fmt_box.bind("<<ComboboxSelected>>", lambda _e: self._on_test_format())
        ttk.Label(top, text=t("test.model")).pack(side="left")
        self.test_model_var = tk.StringVar(value=self.cfg.get("PROXY_DEFAULT_MODEL") or "sonnet")
        ttk.Combobox(top, textvariable=self.test_model_var, values=MODELS, width=14).pack(side="left", padx=6)
        self.btn_send = ttk.Button(top, text=t("test.send"), command=self.send_test_prompt)
        self.btn_send.pack(side="left", padx=(12, 4))
        self.btn_tooltest = ttk.Button(top, text=t("test.tool"), command=self.send_tool_test)
        self.btn_tooltest.pack(side="left")
        self.test_status = ttk.Label(top, text="", style="Help.TLabel")
        self.test_status.pack(side="left", padx=12)

        ttk.Label(tab, text=t("test.prompt")).pack(anchor="w", pady=(10, 2))
        self.test_prompt = tk.Text(tab, height=5, font=("Segoe UI", 10), wrap="word")
        self.test_prompt.insert("1.0", t("test.default_prompt"))
        self.test_prompt.pack(fill="x")
        self.test_output_label = ttk.Label(tab)
        self.test_output_label.pack(anchor="w", pady=(10, 2))
        self.test_output = ScrolledText(tab, font=("Consolas", 9), wrap="word")
        self.test_output.pack(fill="both", expand=True)
        self._on_test_format()

    def _build_howto_tab(self) -> None:
        tab = ttk.Frame(self.notebook, padding=4)
        self.notebook.add(tab, text=t("tab.howto"))
        text = ScrolledText(tab, wrap="word", font=("Segoe UI", 10), relief="flat",
                            background=self.cget("background"), padx=16, pady=10)
        text.pack(fill="both", expand=True)
        text.tag_configure("h", font=("Segoe UI", 12, "bold"), spacing1=14, spacing3=6)
        text.tag_configure("p", spacing3=6)
        text.tag_configure("m", font=("Consolas", 10), foreground="#1f4e8c", spacing1=4, spacing3=8)
        text.tag_configure("s", lmargin1=8, lmargin2=34, spacing3=6)
        text.tag_configure("b", lmargin1=8, lmargin2=24, spacing3=4)
        for style, content in HOWTO[_lang]:
            text.insert("end", ("•  " + content if style == "b" else content) + "\n", style)
        text.configure(state="disabled")

    def _on_test_format(self) -> None:
        fmt = self.test_format_var.get()
        self.test_output_label.configure(text=t("test.output", path=TEST_FORMATS[fmt][0]))
        # The legacy endpoint is prompt-in/text-out and has no tools.
        self.btn_tooltest.state(["disabled"] if fmt == "OpenAI Legacy" else ["!disabled"])

    # ---- language ---------------------------------------------------------

    def set_language(self, code: str) -> None:
        """Switch the UI language: persist just that choice, then rebuild all
        widgets while keeping unsaved field edits, the log and the open tab."""
        global _lang
        stored = read_config_file()
        stored["UI_LANGUAGE"] = code
        save_config(stored)
        self.cfg["UI_LANGUAGE"] = code
        new_lang = resolve_language(code)
        if new_lang == _lang:
            return

        fields = {k: v.get() for k, v in self.field_vars.items()}
        autostart = self.autostart_var.get()
        log = self.log_text.get("1.0", "end-1c")
        tab = self.notebook.index("current")
        test_format = self.test_format_var.get()
        test_model = self.test_model_var.get()
        key_visible = self.key_entry.cget("show") == ""

        _lang = new_lang
        for child in self.winfo_children():
            child.destroy()
        self._build_ui()

        for k, v in fields.items():
            self.field_vars[k].set(v)
        self.autostart_var.set(autostart)
        self._set_key_visible(key_visible)
        if log:
            self.log_text.insert("1.0", log + "\n")
            self.log_text.see("end")
        self.test_format_var.set(test_format)
        self.test_model_var.set(test_model)
        self._on_test_format()
        self.notebook.select(tab)
        if self._requests:
            self._render_requests(list(self._requests.values()))

    # ---- actions ----------------------------------------------------------

    def _collect_settings(self, quiet: bool = False) -> dict | None:
        """Form values as a config dict, or None (after an error dialog unless
        `quiet`) when something is invalid."""
        cfg = dict(self.cfg)

        def fail(title: str, message: str) -> None:
            if not quiet:
                messagebox.showerror(title, message)

        for key, default, kind in FIELDS:
            label = t(f"f.{key}")
            value = self.field_vars[key].get().strip()
            if kind == "int" and not value.isdigit():
                return fail(t("dlg.invalid"), t("dlg.int", label=label))
            if kind == "ports":
                ports = split_keys(value)
                if not all(p.isdigit() and 0 < int(p) < 65536 for p in ports):
                    return fail(t("dlg.invalid"), t("dlg.ports", label=label))
                value = ", ".join(ports)
            if kind == "secret" and value:
                keys = split_keys(value)
                if any(len(k) < 16 for k in keys):
                    return fail(t("dlg.key_short_title"), t("dlg.key_short"))
                value = ", ".join(keys)
            cfg[key] = value or default
        cfg["UI_AUTOSTART"] = bool(self.autostart_var.get())
        return cfg

    def _is_dirty(self) -> bool:
        for key, default, kind in FIELDS:
            saved = str(self.cfg.get(key, default))
            current = self.field_vars[key].get().strip()
            if kind == "bool":
                saved, current = str(is_true(saved)), str(is_true(current))
            elif kind in ("secret", "ports"):
                saved, current = ",".join(split_keys(saved)), ",".join(split_keys(current))
            if current != saved and not (current == "" and saved == default):
                return True
        return bool(self.autostart_var.get()) != bool(self.cfg.get("UI_AUTOSTART"))

    def _refresh_dirty(self) -> None:
        if not hasattr(self, "dirty_label"):
            return  # still building the form
        if self._is_dirty():
            self.dirty_label.configure(text=t("settings.dirty"), style="Dirty.TLabel")
        else:
            self.dirty_label.configure(text=t("settings.clean"), style="Clean.TLabel")

    def save_settings(self, restart: bool = False) -> None:
        cfg = self._collect_settings()
        if cfg is None:
            return
        if cfg["PROXY_HOST"] not in LOOPBACK_HOSTS and not first_key(cfg):
            answer = messagebox.askyesnocancel(t("dlg.net_nokey_title"),
                                               t("dlg.net_nokey", host=cfg["PROXY_HOST"]))
            if answer is None:
                return
            if answer:
                cfg["PROXY_API_KEY"] = new_api_key()
                self.field_vars["PROXY_API_KEY"].set(cfg["PROXY_API_KEY"])
                self._set_key_visible(True)
        cfg["UI_LANGUAGE"] = self.cfg.get("UI_LANGUAGE", "auto")
        save_config(cfg)
        self.cfg = cfg
        self._refresh_base_url()
        self._refresh_howto()
        self._refresh_access_badge()
        self._refresh_dirty()
        self.log_queue.put(t("log.saved", path=CONFIG_FILE))
        if restart:
            self.restart_proxy()
        elif self.proxy.running:
            if messagebox.askyesno(t("dlg.restart_title"), t("dlg.restart")):
                self.restart_proxy()

    def reset_defaults(self) -> None:
        for key, var in self.field_vars.items():
            var.set(DEFAULTS[key])

    def _set_key_visible(self, visible: bool) -> None:
        self.key_entry.configure(show="" if visible else "•")
        self.key_show_btn.configure(text=t("key.hide") if visible else t("key.show"))

    def toggle_key_visible(self) -> None:
        self._set_key_visible(self.key_entry.cget("show") != "")

    def generate_key(self) -> None:
        if self.field_vars["PROXY_API_KEY"].get().strip() and not messagebox.askyesno(
                t("dlg.newkey_title"), t("dlg.newkey")):
            return
        self.field_vars["PROXY_API_KEY"].set(new_api_key())
        self._set_key_visible(True)
        self.log_queue.put(t("log.key_generated"))

    def copy_key(self) -> None:
        key = first_key({"PROXY_API_KEY": self.field_vars["PROXY_API_KEY"].get()})
        if not key:
            messagebox.showinfo(t("dlg.nokey_title"), t("dlg.nokey"))
            return
        self.clipboard_clear()
        self.clipboard_append(key)
        self.log_queue.put(t("log.key_copied"))

    def start_proxy(self) -> None:
        if self.proxy.running:
            return
        if self.health_ok:
            messagebox.showwarning(t("dlg.port_busy_title"),
                                   t("dlg.port_busy", url=local_base(self.cfg)))
            return
        try:
            self.proxy.start()
        except OSError as exc:
            messagebox.showerror(t("dlg.start_failed"), str(exc))
            return
        self.notebook.select(self.TAB_LOG)

    def stop_proxy(self) -> None:
        self.proxy.stop()
        self.health_ok = False

    def restart_proxy(self) -> None:
        self.stop_proxy()
        self.after(500, self.start_proxy)

    def copy_base_url(self) -> None:
        self.clipboard_clear()
        self.clipboard_append(self.base_url_var.get())
        self.log_queue.put(t("log.copied", text=self.base_url_var.get()))

    def show_request_details(self, _event=None) -> None:
        sel = self.req_tree.selection()
        if not sel or sel[0] not in self._requests:
            return
        rid = sel[0]
        win = tk.Toplevel(self)
        win.title(t("det.title", id=rid))
        win.geometry("980x640")
        ttk.Label(win, text=t("det.flow"), font=("Consolas", 11, "bold"),
                  padding=(12, 10, 12, 4)).pack(anchor="w")
        loading = ttk.Label(win, text=t("det.loading"), padding=12)
        loading.pack(anchor="w")
        url = f"{local_base(self.cfg)}/requests/{rid}"
        key = first_key(self.cfg)
        fallback = self._requests[rid]

        def worker() -> None:
            try:
                data, error = http_json(url, timeout=5, api_key=key), None
            except Exception as exc:  # noqa: BLE001 — shown in the window
                data, error = dict(fallback), str(exc)
            self.after(0, lambda: self._fill_details(win, loading, data, error))

        threading.Thread(target=worker, daemon=True).start()

    def _fill_details(self, win: tk.Toplevel, loading: ttk.Label, data: dict,
                      error: str | None) -> None:
        if not win.winfo_exists():
            return
        loading.destroy()
        details = data.get("details") or {}
        nb = ttk.Notebook(win)
        nb.pack(fill="both", expand=True, padx=10, pady=(4, 10))

        def add_tab(title: str, help_text: str, body: str, mono: bool = True) -> None:
            frame = ttk.Frame(nb, padding=8)
            nb.add(frame, text=title)
            if help_text:
                ttk.Label(frame, text=help_text, style="Help.TLabel", wraplength=900,
                          justify="left").pack(anchor="w", pady=(0, 6))
            box = ScrolledText(frame, font=("Consolas", 10) if mono else ("Segoe UI", 10), wrap="word")
            box.pack(fill="both", expand=True)
            box.insert("1.0", body)
            box.configure(state="disabled")

        overview = [
            (t("col.time"), fmt_ts(data.get("time"), with_date=True)),
            (t("col.api"), API_LABELS.get(data.get("api"), data.get("api") or "–")),
            (t("det.path"), data.get("path", "–")),
            (t("col.model"), data.get("model", "–")),
            (t("col.stream"), t("yes") if data.get("stream") else t("no")),
            (t("col.msgs"), data.get("msgs", "–")),
            (t("col.tools"), data.get("tools", "–")),
            (t("col.tool_calls"), ", ".join(data.get("tool_calls") or []) or "–"),
            (t("col.duration"), f"{data['duration_s']:.1f}s" if "duration_s" in data else "…"),
            (t("col.status"), data.get("status", "–")),
        ]
        if data.get("error"):
            overview.append((t("det.error"), data["error"]))
        if details.get("cli_command"):
            overview.append((t("det.cli"), details["cli_command"]))
        if error:
            overview.append(("!", t("det.fetch_error", err=error)))
        width = max(len(str(k)) for k, _ in overview) + 2
        add_tab(t("det.tab.overview"), "",
                "\n".join(f"{str(k) + ':':<{width}}{v}" for k, v in overview))
        for part in ("client_request", "system_prompt", "prompt", "raw_output", "result"):
            body = details.get(part)
            add_tab(t(f"det.tab.{part}"), t(f"det.help.{part}"),
                    body if body else t("det.none"))

    def _run_test(self, prompt: str, with_tool: bool) -> None:
        if not self.health_ok:
            messagebox.showinfo(t("dlg.not_reachable_title"), t("dlg.not_reachable"))
            return
        fmt = self.test_format_var.get()
        path, build = TEST_FORMATS[fmt]
        payload = build(prompt, self.test_model_var.get().strip() or "sonnet", with_tool)
        label = f"{fmt} · {t('test.kind_tool') if with_tool else t('test.kind_prompt')}"
        self.btn_send.state(["disabled"])
        self.btn_tooltest.state(["disabled"])
        self.test_status.configure(text=t("test.running", label=label))
        self.test_output.delete("1.0", "end")
        url = local_base(self.cfg) + path
        timeout = float(self.cfg.get("PROXY_SUBPROCESS_TIMEOUT") or 1800)
        api_key = first_key(self.cfg)

        def worker() -> None:
            started = time.monotonic()
            try:
                result = http_json(url, payload, timeout=timeout, api_key=api_key)
                error = None
            except urllib.error.HTTPError as exc:
                result, error = None, f"HTTP {exc.code}: {exc.read().decode('utf-8', errors='replace')}"
            except Exception as exc:  # noqa: BLE001 — shown to the user verbatim
                result, error = None, repr(exc)
            self.result_queue.put(("test", fmt, label, with_tool, time.monotonic() - started,
                                   result, error))

        threading.Thread(target=worker, daemon=True).start()

    def send_test_prompt(self) -> None:
        prompt = self.test_prompt.get("1.0", "end").strip()
        if prompt:
            self._run_test(prompt, with_tool=False)

    def send_tool_test(self) -> None:
        self._run_test(t("test.tool_prompt"), with_tool=True)

    def on_close(self) -> None:
        if self._is_dirty() and not messagebox.askyesno(t("dlg.quit_title"), t("dlg.unsaved")):
            return
        # The proxy's output is piped into this window, so it can't outlive it.
        if self.proxy.running:
            if not messagebox.askokcancel(t("dlg.quit_title"), t("dlg.quit")):
                return
            self.proxy.stop()
        self.destroy()

    # ---- refresh ----------------------------------------------------------

    def _refresh_base_url(self) -> None:
        self.base_url_var.set(local_base(self.cfg) + "/v1")

    def _refresh_access_badge(self) -> None:
        """Header badge: who can reach the proxy, and whether a key protects it."""
        host = str(self.cfg.get("PROXY_HOST") or "127.0.0.1")
        if host in LOOPBACK_HOSTS:
            text, bg, fg = t("badge.local"), "#e8e8e8", "#444444"
        elif first_key(self.cfg):
            text, bg, fg = t("badge.net_key"), "#dafbe1", "#116329"
        else:
            text, bg, fg = t("badge.net_open"), "#ffebe9", "#cf222e"
        self.access_label.configure(text=text, bg=bg, fg=fg)

    def _refresh_howto(self) -> None:
        model = self.cfg.get("PROXY_DEFAULT_MODEL") or "sonnet"
        others = " / ".join(m for m in MODELS if m != model)
        base = local_base(self.cfg)
        extra = str(self.cfg.get("PROXY_EXTRA_PORTS") or "").strip()
        lines = [
            f"{t('howto.openai'):<52}{base}/v1",
            f"{t('howto.lmstudio'):<52}{base}/v1",
            f"{t('howto.responses'):<52}{base}/v1",
            f"{t('howto.anthropic'):<52}{base}   {t('howto.no_v1')}",
            f"{t('howto.ollama'):<52}{base}",
        ]
        remote = network_bases(self.cfg)
        if remote:
            lines.append(f"{t('howto.remote'):<52}" + "  ·  ".join(remote))
        lines.append("")
        if first_key(self.cfg):
            lines.append(t("howto.key_set"))
            if is_true(self.cfg.get("PROXY_AUTH_EXEMPT_LOCALHOST")):
                lines.append(t("howto.key_local"))
        else:
            lines.append(t("howto.key_none"))
        lines.append(t("howto.model", model=model, others=others))
        if extra:
            lines.append(t("howto.extra", ports=extra))
        self.howto_var.set("\n".join(lines))

    def _set_status(self, text: str, color: str) -> None:
        self.status_label.configure(text=text)
        self.status_dot.itemconfigure(self._dot, fill=color)

    def _update_status(self) -> None:
        ours = self.proxy.running
        if self.health_ok and self.auth_mismatch:
            self._set_status(t("status.key_changed"), "#d29922")
        elif ours and self.health_ok:
            self._set_status(t("status.running"), "#2ea043")
        elif ours:
            self._set_status(t("status.starting"), "#d29922")
        elif self.health_ok:
            self._set_status(t("status.external"), "#1f6feb")
        elif self.proxy.exit_code not in (None, 0):
            self._set_status(t("status.crashed", code=self.proxy.exit_code), "#cf222e")
        else:
            self._set_status(t("status.stopped"), "#999999")
        self.btn_start.state(["disabled"] if ours or self.health_ok else ["!disabled"])
        self.btn_stop.state(["!disabled"] if ours else ["disabled"])
        self.btn_restart.state(["!disabled"] if ours else ["disabled"])

    def _append_log(self, line: str) -> None:
        tag = None
        if line.startswith("[UI]"):
            tag = "ui"
        elif " ERROR " in line or "Traceback" in line or line.startswith(("OSError", "  File")):
            tag = "error"
        elif " WARNING " in line:
            tag = "warn"
        elif "extracted" in line:
            tag = "tool"
        self.log_text.insert("end", line + "\n", tag)
        lines = int(self.log_text.index("end-1c").split(".")[0])
        if lines > 5000:
            self.log_text.delete("1.0", f"{lines - 5000}.0")
        if self.autoscroll_var.get():
            self.log_text.see("end")

    def _render_requests(self, records: list[dict]) -> None:
        self._requests = {r["id"]: r for r in records if "id" in r}
        selected = self.req_tree.selection()
        self.req_tree.delete(*self.req_tree.get_children())
        for r in records:
            status = r.get("status")
            tags = ()
            if r.get("error") or (isinstance(status, int) and status >= 400):
                tags = ("error",)
            elif status == "running":
                tags = ("running",)
            elif r.get("tool_calls"):
                tags = ("tool",)
            api = API_LABELS.get(r.get("api"), r.get("path", ""))
            self.req_tree.insert("", "end", iid=r["id"], tags=tags, values=(
                fmt_ts(r.get("time")), api, r.get("model", ""),
                t("yes") if r.get("stream") else t("no"), r.get("msgs", ""), r.get("tools", ""),
                ", ".join(r.get("tool_calls") or []),
                f"{r['duration_s']:.1f}s" if "duration_s" in r else "…",
                t("running") if status == "running" else status, r.get("preview", ""),
            ))
        existing = [s for s in selected if self.req_tree.exists(s)]
        if existing:
            self.req_tree.selection_set(existing)

    def _render_quota(self, q: dict) -> None:
        def put(key: str, value: str) -> None:
            self.quota_vars[key].set(value)

        status = q.get("status") or "–"
        put("status", {"allowed": "OK (allowed)"}.get(status, f"⚠ {status}"))
        put("rate_limit_type", str(q.get("rate_limit_type") or "–"))
        resets = float(q.get("resets_at") or 0)
        if resets:
            mins = int((resets - time.time()) / 60)
            stamp = fmt_ts(resets, True)
            put("resets_at", f"{stamp}  ({t('quota.in_min', m=mins)})" if mins >= 0 else stamp)
        else:
            put("resets_at", "–")
        cfg = q.get("config") or {}
        put("session_turns", f"{q.get('session_turns', 0)}  "
                             f"({t('quota.warn_from', n=cfg.get('heavy_turns', '?'))})")
        put("session_cost_usd", f"{float(q.get('session_cost_usd') or 0):.2f}")
        put("session_started_at", fmt_ts(q.get("session_started_at"), True))
        put("is_using_overage", t("yes") if q.get("is_using_overage") else t("no"))
        put("last_event_at", fmt_ts(q.get("last_event_at"), True))

    def _render_test(self, fmt: str, label: str, with_tool: bool, duration: float,
                     result, error) -> None:
        self.btn_send.state(["!disabled"])
        self._on_test_format()  # re-enables the tool button where it applies
        if error:
            self.test_status.configure(text=t("test.error", label=label, s=duration))
            self.test_output.insert("1.0", error)
            return
        verdict = ""
        if with_tool:
            verdict = t("test.tool_ok") if _has_tool_call(fmt, result) else t("test.tool_missing")
        self.test_status.configure(text=t("test.done", label=label, s=duration, verdict=verdict))
        self.test_output.insert("1.0", json.dumps(result, indent=2, ensure_ascii=False))

    def _fetch_state(self) -> None:
        base = local_base(self.cfg)
        key = first_key(self.cfg)
        state: dict = {"health": False, "auth_error": False}
        try:
            http_json(base + "/health", timeout=1.5)
            state["health"] = True
            state["requests"] = http_json(base + "/requests", timeout=2, api_key=key)
            state["quota"] = http_json(base + "/quota", timeout=2, api_key=key)
        except urllib.error.HTTPError as exc:
            # Running proxy still uses a different key than the saved settings.
            state["auth_error"] = exc.code == 401
        except Exception:  # noqa: BLE001 — unreachable/old proxy just shows as offline
            pass
        self.result_queue.put(("state", state))

    def poll(self) -> None:
        try:
            for _ in range(500):
                self._append_log(self.log_queue.get_nowait())
        except queue.Empty:
            pass

        try:
            while True:
                item = self.result_queue.get_nowait()
                if item[0] == "state":
                    self._fetching = False
                    state = item[1]
                    self.health_ok = state["health"]
                    self.auth_mismatch = state["auth_error"]
                    if "requests" in state:
                        self._render_requests(state["requests"])
                    if "quota" in state:
                        self._render_quota(state["quota"])
                elif item[0] == "test":
                    self._render_test(*item[1:])
        except queue.Empty:
            pass

        now = time.monotonic()
        if not self._fetching and now - self._last_fetch >= self.FETCH_EVERY_S:
            self._fetching = True
            self._last_fetch = now
            threading.Thread(target=self._fetch_state, daemon=True).start()

        self._update_status()
        self.after(self.POLL_MS, self.poll)


def main() -> None:
    if os.name == "nt":
        try:  # crisp text on high-DPI displays
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:  # noqa: BLE001
            pass
    App().mainloop()


if __name__ == "__main__":
    main()
