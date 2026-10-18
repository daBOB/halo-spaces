#!/usr/bin/env python3
"""Automatischer Test von halo.spaces in einem frischen, temporären Thunderbird-Profil.

Legt ein leeres Profil an (keine Konten, keine echten Daten), installiert das
gebaute Paket, startet Thunderbird mit Marionette und prüft die Stellen, an
denen es früher Rückschritte gab: Listenbreite, Toolbar-Kanten und Kapseln,
Wochenansicht, Schnellfenster, Emojis, Trennlinien (Ziehen/Doppelklick),
Status-Symbol in der Kalenderliste und das Fehlerprotokoll.

Aufruf:  python3 tests/halo_test.py [--xpi dist/…xpi] [--keep] [--dark] [--shots ORDNER] [--tb …]
         (ohne --xpi wird vorher ./build.sh ausgeführt)
Ergebnis: eine Zeile je Prüfung (OK/FEHLER), Rückgabewert 0 nur wenn alles OK ist.
"""

import argparse
import glob
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time

ADDON_ID = "mail-cards@sebastian.local"
PORT = 2839
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_TB = {
    "darwin": "/Applications/Thunderbird.app/Contents/MacOS/thunderbird",
    "win32": r"C:\Program Files\Mozilla Thunderbird\thunderbird.exe",
}.get(sys.platform, "thunderbird")


class Marionette:
    def __init__(self, port):
        self.sock = socket.create_connection(("127.0.0.1", port))
        # nie endlos warten: ein Befehl darf höchstens 11 Minuten dauern (--perf)
        self.sock.settimeout(660)
        self.id = 0
        self._read()
        self.cmd("WebDriver:NewSession", {"capabilities": {}})
        self.cmd("Marionette:SetContext", {"value": "chrome"})

    def _recv(self, n):
        chunk = self.sock.recv(n)
        if not chunk:  # Thunderbird hat die Verbindung geschlossen (beendet/abgestürzt)
            raise ConnectionError("Marionette-Verbindung geschlossen")
        return chunk

    def _read(self):
        buf = b""
        while b":" not in buf:
            buf += self._recv(1)
        n, rest = buf.split(b":", 1)
        n = int(n)
        while len(rest) < n:
            rest += self._recv(n - len(rest))
        return json.loads(rest)

    def cmd(self, name, params=None):
        self.id += 1
        data = json.dumps([0, self.id, name, params or {}]).encode()
        self.sock.sendall(str(len(data)).encode() + b":" + data)
        reply = self._read()
        if reply[2]:
            raise RuntimeError(reply[2].get("message", reply[2]))
        return reply[3]

    def js(self, body):
        script = "return (async () => {" + PRELUDE + body + "})();"
        return self.cmd("WebDriver:ExecuteScript", {"script": script, "args": []}).get("value")

    def pointer(self, steps):
        self.cmd("WebDriver:PerformActions", {"actions": [
            {"type": "pointer", "id": "maus", "parameters": {"pointerType": "mouse"}, "actions": steps}]})
        self.cmd("WebDriver:ReleaseActions", {})


# gemeinsame Hilfen für alle Prüfungen (im Chrome-Kontext von Thunderbird)
PRELUDE = r"""
const w = Services.wm.getMostRecentWindow("mail:3pane"); const d = w.document;
const wait = ms => new Promise(r => w.setTimeout(r, ms));
// wartet, bis fn() etwas liefert (statt fester Wartezeiten – bei hoher Last zuverlässiger)
const until = async (fn, ms = 6000) => { for (let t = 0; t < ms; t += 100) { const v = fn(); if (v) return v; await wait(100); } return fn(); };
const tm = d.getElementById("tabmail");
const { cal } = ChromeUtils.importESModule("resource:///modules/calendar/calUtils.sys.mjs");
const toMail = async () => { tm.switchToTab(0); await wait(900); return tm.currentAbout3Pane; };
const toCalendar = async () => { tm.openTab("calendar", {}); await wait(1500); };
const testCalendar = () => cal.manager.getCalendars().find(c => c.name == "Halo Test");
const items = async title => { const c = testCalendar(); return (await c.getItemsAsArray(Ci.calICalendar.ITEM_FILTER_ALL_ITEMS, 0, null, null)).filter(i => i.title == title); };
const rect = el => { const r = el.getBoundingClientRect(); return { l: Math.round(r.left), r: Math.round(r.right), w: Math.round(r.width) }; };
"""


def check(name, fn, results):
    try:
        ok, detail = fn()
    except Exception as e:  # Fehler in der Prüfung selbst zählen als Fehlschlag
        ok, detail = False, f"Ausnahme: {e}"
    results.append((name, ok, detail))
    print(f"{'OK     ' if ok else 'FEHLER '} {name}" + (f" – {detail}" if detail else ""), flush=True)


def wait_port(port, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=1).close()
            return True
        except OSError:
            time.sleep(0.5)
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--xpi", help="Paket (Standard: neuestes dist/halo-spaces-*.xpi)")
    ap.add_argument("--tb", default=DEFAULT_TB, help="Thunderbird-Programm")
    ap.add_argument("--keep", action="store_true", help="Testprofil nicht löschen")
    ap.add_argument("--tb-log", help="Ausgabe von Thunderbird in diese Datei schreiben (zur Fehlersuche)")
    ap.add_argument("--dark", action="store_true", help="im Dunkelmodus starten")
    ap.add_argument("--shots", help="Ordner für Screenshots aller Bereiche")
    ap.add_argument("--eval", help="nur dieses JavaScript ausführen (Chrome-Kontext, mit den Hilfen aus PRELUDE) und das Ergebnis ausgeben – zum Untersuchen")
    ap.add_argument("--perf", action="store_true", help="nur die Geschwindigkeit messen (5000 Mails, mit und ohne halo.spaces)")
    ap.add_argument("--compare", help="Ordner mit früheren Screenshots: zeigt, wo sich etwas verändert hat (braucht --shots)")
    args = ap.parse_args()

    if not args.xpi:
        # ohne Angabe immer den aktuellen Stand bauen (sonst testet man ein altes Paket)
        built = subprocess.run([os.path.join(ROOT, "build.sh")], capture_output=True, text=True, cwd=ROOT)
        if built.returncode != 0:
            sys.exit("build.sh fehlgeschlagen:\n" + built.stderr)
    xpi = args.xpi or max((x for x in glob.glob(os.path.join(ROOT, "dist", "halo-spaces-*.xpi")) if not x.endswith("-atn.xpi")),
                           key=os.path.getmtime, default=None)  # nicht die Store-Variante (ohne update_url)
    if not xpi or not os.path.exists(xpi):
        sys.exit("Kein Paket gefunden – zuerst ./build.sh")
    version = json.load(open(os.path.join(ROOT, "extension", "manifest.json")))["version"]

    profile = tempfile.mkdtemp(prefix="halo-test-")
    os.makedirs(os.path.join(profile, "extensions"))
    shutil.copy(xpi, os.path.join(profile, "extensions", f"{ADDON_ID}.xpi"))
    with open(os.path.join(profile, "user.js"), "w") as f:
        for pref, value in {
            "extensions.autoDisableScopes": 0,
            "marionette.port": PORT,
            "mail.shell.checkDefaultClient": False,
            "mail.provider.suppress_dialog_on_startup": True,
            "mail.rights.version": 1,
            "app.update.enabled": False,
            "datareporting.policy.dataSubmissionPolicyBypassNotification": True,
            "toolkit.telemetry.reportingpolicy.firstRun": False,
            "calendar.timezone.useSystemTimezone": True,
            # Zeitgeber im Hintergrund nicht drosseln
            "widget.windows.window_occlusion_tracking.enabled": False,
            "dom.timeout.enable_budget_timer_throttling": False,
            "dom.min_background_timeout_value": 4,
            # das macOS-Adressbuch (echte Kontakte) nicht einbinden
            "ldap_2.servers.osx.position": 0,
            **({"ui.systemUsesDarkTheme": 1} if args.dark else {}),
        }.items():
            f.write(f"user_pref({json.dumps(pref)}, {json.dumps(value)});\n")

    print(f"halo.spaces {version} · Paket {os.path.basename(xpi)} · Profil {profile}", flush=True)
    out = open(args.tb_log, "w") if args.tb_log else subprocess.DEVNULL
    proc = subprocess.Popen([args.tb, "-profile", profile, "-no-remote", "-marionette", "-remote-allow-system-access"],
                            stdout=out, stderr=subprocess.STDOUT if args.tb_log else subprocess.DEVNULL,
                            # ab Thunderbird 159 zusätzlich als Umgebungsvariable nötig
                            env={**os.environ, "MOZ_REMOTE_ALLOW_SYSTEM_ACCESS": "1"})
    results = []
    try:
        if not wait_port(PORT):
            sys.exit("Thunderbird hat Marionette nicht geöffnet")
        time.sleep(8)  # Erweiterung und Fenster fertig aufbauen lassen
        m = Marionette(PORT)
        if args.perf:
            args.eval = open(os.path.join(ROOT, "tests", "perf.js")).read()
        if args.eval:
            m.cmd("WebDriver:SetTimeouts", {"script": 600000})
            print(json.dumps(m.js(args.eval), indent=1, ensure_ascii=False))
            return
        run_checks(m, version, profile, results)
        if args.shots:
            take_shots(m, args.shots, "dark" if args.dark else "light")
            if args.compare:
                compare_shots(m, args.compare, args.shots)
    finally:
        try:
            Marionette(PORT).js("Services.startup.quit(Ci.nsIAppStartup.eForceQuit); return 1;")
        except Exception:
            pass
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
        if not args.keep:
            shutil.rmtree(profile, ignore_errors=True)

    failed = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(failed)} von {len(results)} Prüfungen OK" + (f", {len(failed)} FEHLER" if failed else ""))
    sys.exit(1 if failed else 0)


def take_shots(m, folder, scheme):
    """Screenshots aller Bereiche (Testprofil, keine echten Daten)."""
    import base64
    os.makedirs(folder, exist_ok=True)
    for name, js in [
        ("mail", "await toMail();"),
        ("message", MESSAGE_JS),
        ("message-remote", """const { MailServices } = ChromeUtils.importESModule("resource:///modules/MailServices.sys.mjs"); await toMail(); const f = MailServices.accounts.localFoldersServer.rootFolder.getChildNamed("Schrift");
          const a3 = tm.currentAbout3Pane; a3.displayFolder(f.URI); await wait(800);
          for (let i = 0; i < a3.gDBView.rowCount; i++) if (a3.gDBView.getMsgHdrAt(i).subject == "Schrift 1") a3.threadTree.selectedIndex = i;
          await wait(6000);"""),
        ("mail-1280", "w.resizeTo(1280, 900); await wait(1200); await toMail();"),
        ("calendar-week", 'await toCalendar(); w.switchToView("week"); await wait(800);'),
        ("calendar-month", 'await toCalendar(); w.switchToView("month"); await wait(800);'),
        ("tasks", 'tm.openTab("tasks", {}); await wait(1500);'),
        ("addressbook", 'tm.openTab("addressBookTab", {}); await wait(2000);'),
        ("settings", "w.openPreferencesTab(); await wait(2000);"),
        ("addon-settings", f'w.openAddonsMgr("addons://detail/" + encodeURIComponent("{ADDON_ID}") + "/preferences"); await wait(3000);'),
    ]:
        m.js(("" if "resizeTo" in js else "w.resizeTo(1500, 900); ") + js + " await wait(600); return 1;")
        data = m.cmd("WebDriver:TakeScreenshot", {})["value"]
        path = os.path.join(folder, f"{scheme}-{name}.png")
        open(path, "wb").write(base64.b64decode(data))
        print(f"Screenshot {path}", flush=True)
    # eigene Fenster: Verfassen und Termin-Dialog
    for name, js in [
        ("compose", COMPOSE_JS),
        ("event-dialog", 'await toCalendar(); w.switchToView("week"); w.createEventWithDialog(null, null, null, "Halo Dialog"); await wait(2500); return 1;'),
        ("calendar-properties", 'w.openDialog("chrome://calendar/content/calendar-properties-dialog.xhtml", "_blank", "centerscreen,chrome,titlebar,resizable", { calendar: testCalendar() }); await wait(2000); return 1;'),
    ]:
        handles = lambda: (lambda r: r["value"] if isinstance(r, dict) else r)(m.cmd("WebDriver:GetWindowHandles"))
        main = (lambda r: r["value"] if isinstance(r, dict) else r)(m.cmd("WebDriver:GetWindowHandle"))
        before = set(handles())
        m.js(js)
        new = [h for h in handles() if h not in before]
        if not new:
            print(f"Screenshot {name}: kein Fenster", flush=True)
            continue
        m.cmd("WebDriver:SwitchToWindow", {"handle": new[0]})
        m.cmd("WebDriver:ExecuteScript", {"script": "window.resizeTo(900, 700); return new Promise(r => setTimeout(r, 800));", "args": []})
        data = m.cmd("WebDriver:TakeScreenshot", {})["value"]
        path = os.path.join(folder, f"{scheme}-{name}.png")
        open(path, "wb").write(base64.b64decode(data))
        print(f"Screenshot {path}", flush=True)
        m.cmd("WebDriver:ExecuteScript", {"script": "window.close(); return 1;", "args": []})
        m.cmd("WebDriver:SwitchToWindow", {"handle": main})


# eine Test-Mail (example.invalid) in Local Folders öffnen – für die Leseansicht
MESSAGE_JS = r"""
  const { MailServices } = ChromeUtils.importESModule("resource:///modules/MailServices.sys.mjs");
  try { MailServices.accounts.localFoldersServer; } catch { MailServices.accounts.createLocalMailAccount(); await wait(500); }
  const root = MailServices.accounts.localFoldersServer.rootFolder;
  if (!root.containsChildNamed("Lesen")) { root.createSubfolder("Lesen", null); await wait(800); }
  const f = root.getChildNamed("Lesen");
  if (!f.getTotalMessages(false)) {
    f.QueryInterface(Ci.nsIMsgLocalMailFolder).addMessage(unescape(encodeURIComponent("From - Sat Oct 17 10:00:00 2026\r\nFrom: Anna Beispiel <anna@example.invalid>\r\nTo: Halo Test <halo@example.invalid>\r\nCc: Ben Muster <ben@example.invalid>\r\nSubject: Treffen am Donnerstag\r\nDate: Sat, 17 Oct 2026 09:41:00 +0200\r\nMessage-ID: <halo-read@example.invalid>\r\nList-ID: Halo Neuigkeiten <news.example.invalid>\r\nList-Unsubscribe: <https://example.invalid/abmelden?id=1>, <mailto:abmelden@example.invalid?subject=unsubscribe>\r\nContent-Type: text/plain; charset=UTF-8\r\n\r\nHallo,\r\n\r\npasst dir Donnerstag um 10 Uhr? Ich bringe die Unterlagen mit.\r\n\r\nLiebe Grüße\r\nAnna\r\n")));
    await wait(800);
  }
  const a3 = await toMail(); a3.displayFolder(f.URI); await wait(800);
  a3.threadTree.selectedIndex = 0; await wait(2000);"""


# Verfassen-Fenster mit einer reinen Test-Identität (example.invalid, kein Server)
COMPOSE_JS = r"""
  const { MailServices } = ChromeUtils.importESModule("resource:///modules/MailServices.sys.mjs");
  try { MailServices.accounts.localFoldersServer; } catch { MailServices.accounts.createLocalMailAccount(); await wait(500); }
  const acc = MailServices.accounts.findAccountForServer(MailServices.accounts.localFoldersServer);
  if (!acc.identities.length) {
    const id = MailServices.accounts.createIdentity();
    id.fullName = "Halo Test"; id.email = "halo@example.invalid";
    acc.addIdentity(id);
  }
  const params = Cc["@mozilla.org/messengercompose/composeparams;1"].createInstance(Ci.nsIMsgComposeParams);
  const fields = Cc["@mozilla.org/messengercompose/composefields;1"].createInstance(Ci.nsIMsgCompFields);
  fields.to = "Anna Beispiel <anna@example.invalid>"; fields.subject = "Halo Entwurf";
  fields.body = "<p>Hallo Anna,</p><p>das ist ein Testentwurf.</p>";
  params.composeFields = fields; params.identity = acc.identities[0]; params.format = Ci.nsIMsgCompFormat.HTML;
  MailServices.compose.OpenComposeWindowWithParams(null, params);
  await wait(3000); return 1;"""


def compare_shots(m, before, after):
    """Vergleicht Screenshots Pixel für Pixel (in Thunderbird selbst, ohne Bildbibliothek).

    Nur zur Information, kein Fehlschlag: Tagesdatum, Uhrzeit und „in 18 min“
    ändern sich von selbst. Für jede Abweichung entsteht ein Bild …-diff.png,
    in dem die geänderten Stellen rot markiert sind.
    """
    import base64
    print("\nVergleich mit", before, flush=True)
    for path in sorted(glob.glob(os.path.join(after, "*.png"))):
        name = os.path.basename(path)
        if name.endswith("-diff.png"):
            continue
        old = os.path.join(before, name)
        if not os.path.exists(old):
            print(f"  neu       {name}", flush=True)
            continue
        res = m.js(f"""
          const load = async p => createImageBitmap(new w.Blob([await IOUtils.read(p)], {{ type: "image/png" }}));
          const [a, b] = [await load({json.dumps(os.path.abspath(old))}), await load({json.dumps(os.path.abspath(path))})];
          if (a.width != b.width || a.height != b.height) return {{ size: [a.width, a.height, b.width, b.height] }};
          const pixels = img => {{ const c = d.createElementNS("http://www.w3.org/1999/xhtml", "canvas"); c.width = img.width; c.height = img.height;
                                  const x = c.getContext("2d"); x.drawImage(img, 0, 0); return [c, x, x.getImageData(0, 0, c.width, c.height)]; }};
          const [, , pa] = pixels(a); const [cb, xb, pb] = pixels(b);
          const A = pa.data, B = pb.data, out = pb;
          let changed = 0, l = 1e9, t = 1e9, r = -1, bo = -1;
          for (let i = 0; i < A.length; i += 4) {{
            const diff = Math.max(Math.abs(A[i] - B[i]), Math.abs(A[i + 1] - B[i + 1]), Math.abs(A[i + 2] - B[i + 2]));
            if (diff > 40) {{
              changed++; const p = i / 4, px = p % b.width, py = (p - px) / b.width;
              l = Math.min(l, px); r = Math.max(r, px); t = Math.min(t, py); bo = Math.max(bo, py);
              out.data[i] = 255; out.data[i + 1] = 0; out.data[i + 2] = 0;
            }} else {{
              // unverändert: blass, damit die roten Stellen auffallen
              for (const k of [0, 1, 2]) out.data[i + k] = 255 - (255 - B[i + k]) * 0.25;
            }}
          }}
          if (!changed) return {{ changed: 0 }};
          xb.putImageData(out, 0, 0);
          return {{ changed: changed / (b.width * b.height), box: [l, t, r, bo], png: cb.toDataURL("image/png") }};""")
        if "size" in res:
            print(f"  Größe     {name}: {res['size'][0]}×{res['size'][1]} → {res['size'][2]}×{res['size'][3]}", flush=True)
        elif not res["changed"]:
            print(f"  gleich    {name}", flush=True)
        else:
            diff = path[:-4] + "-diff.png"
            open(diff, "wb").write(base64.b64decode(res["png"].split(",", 1)[1]))
            l, t, r, b = res["box"]
            print(f"  geändert  {name}: {res['changed'] * 100:.2f} % der Fläche, Bereich x {l}–{r}, y {t}–{b} → {os.path.basename(diff)}", flush=True)


def run_checks(m, version, profile, results):
    # Vorbereitung: Fenstergröße, Testkalender, Fenster ohne Dialoge
    m.js("""
      for (const x of [...Services.wm.getEnumerator(null)]) if (!x.location.href.includes("messenger.xhtml")) x.close();
      w.moveTo(0, 0); w.resizeTo(1500, 900); await wait(500);
      // Marionette sperrt Add-on-Updates – für die Prüfung „Updates beim Start“ erlauben
      Services.prefs.setBoolPref("extensions.update.enabled", true);
      // Aktionen beim Überfahren sind standardmäßig aus – für die Prüfungen einschalten
      Services.prefs.setBoolPref("extensions.mailcards.hoverActions", true);
      if (!testCalendar()) { const c = cal.manager.createCalendar("storage", Services.io.newURI("moz-storage-calendar://")); c.name = "Halo Test"; c.setProperty("color", "#FF9500"); cal.manager.registerCalendar(c); }
      return 1;""")

    def addon():
        v = m.js("""const { AddonManager } = ChromeUtils.importESModule("resource://gre/modules/AddonManager.sys.mjs");
                    const a = await AddonManager.getAddonByID("%s"); return [a?.version, a?.isActive];""" % ADDON_ID)
        return v == [version, True], f"{v}"
    check("Erweiterung aktiv und richtige Version", addon, results)

    def list_width():
        res = m.js("""
          const out = {};
          for (const width of [1500, 2000, 2600]) { w.resizeTo(width, 900); await wait(1300);
            const a3 = await toMail(); out[width] = Math.round(a3.document.getElementById("threadPane").getBoundingClientRect().width); }
          w.resizeTo(1500, 900); await wait(800); return out;""")
        ok = all(abs(v - 460) <= 1 for v in res.values())
        return ok, f"Listenbreite je Fensterbreite {res} (soll 460)"
    check("Mailliste behält ihre Breite", list_width, results)

    def toolbar_edges():
        res = m.js("""
          const out = [];
          for (const width of [1280, 1500, 2400]) { w.resizeTo(width, 900); await wait(1300);
            const a3 = await toMail(); const off = a3.browsingContext.embedderElement.getBoundingClientRect();
            const row = d.getElementById("unifiedToolbarContainer"); const rl = row.getBoundingClientRect().left;
            const paneRight = Math.round(off.left + a3.document.getElementById("threadPane").getBoundingClientRect().right);
            const lineVar = parseFloat(getComputedStyle(row).getPropertyValue("--mc-list-right"));
            const spacer = d.querySelector('#unifiedToolbarContent > li[item-id="spacer"]').getBoundingClientRect().right;
            const search = Math.round(d.querySelector('#unifiedToolbarContent > li[item-id="search-bar"]').getBoundingClientRect().width);
            out.push({ width, line: Math.round(lineVar + rl) == paneRight || Math.abs(lineVar + rl - paneRight) <= 2, buttonsRightOfList: spacer >= paneRight, search }); }
          w.resizeTo(1500, 900); await wait(800); return out;""")
        ok = all(r["line"] and r["buttonsRightOfList"] and r["search"] >= 159 for r in res)
        detail = " · ".join(f"{r['width']}px: Linie {'✓' if r['line'] else '✗'}, Knöpfe {'✓' if r['buttonsRightOfList'] else '✗'}, Suche {r['search']}" for r in res)
        return ok, detail
    check("Toolbar: Trennlinie, Knopfkante, Suchfeld", toolbar_edges, results)

    def capsules():
        res = m.js("""
          w.resizeTo(1280, 900); await wait(1300); await toMail();
          const GROUPS = [["get-messages","write-message"],["reply","reply-all","forward-inline"],["archive","delete","junk"],["move-to"],["mark-as"]];
          const bad = [];
          for (const g of GROUPS) {
            const vis = g.map(id => d.querySelector(`#unifiedToolbarContent > li[item-id="${id}"]`)).filter(li => li && li.getBoundingClientRect().width);
            if (!vis.length) continue;
            if (getComputedStyle(vis[0]).borderStartStartRadius != "999px") bad.push(vis[0].getAttribute("item-id") + " links eckig");
            if (getComputedStyle(vis.at(-1)).borderStartEndRadius != "999px") bad.push(vis.at(-1).getAttribute("item-id") + " rechts eckig");
          }
          w.resizeTo(1500, 900); await wait(800); return bad;""")
        return not res, ", ".join(res) or "alle Gruppen rund"
    check("Toolbar-Kapseln auch mit ausgeblendeten Knöpfen rund", capsules, results)

    def week_fits():
        res = m.js("""
          w.resizeTo(1280, 900); await wait(800); await toCalendar(); w.switchToView("week"); await wait(1200);
          const week = rect(d.getElementById("week-view")); const tab = rect(d.getElementById("tabmail"));
          const cols = d.querySelectorAll("#week-view .day-column-heading").length;
          w.resizeTo(1500, 900); await wait(600); return { week: week.r, tab: tab.r, cols };""")
        return res["week"] <= res["tab"] + 1 and res["cols"] == 7, f"Woche endet bei {res['week']}, Tab bei {res['tab']}, {res['cols']} Tage"
    check("Wochenansicht passt ins Fenster (alle 7 Tage)", week_fits, results)

    def quick_window():
        res = m.js("""
          await toCalendar();
          const li = [...d.querySelectorAll('#unifiedToolbarContent li[item-id="new-event"]')].find(l => l.getBoundingClientRect().width);
          li.querySelector("button").click(); await wait(900);
          const p = d.getElementById("mcQuickPanel"); const t = p.querySelector(".mc-qc-title");
          p.querySelector(".mc-qc-calsel").value = testCalendar().id; p.querySelector(".mc-qc-calsel").dispatchEvent(new w.Event("change"));
          t.value = "Halo Testtermin";
          t.dispatchEvent(new w.KeyboardEvent("keydown", { key: "Enter", isComposing: true, bubbles: true })); await wait(600);
          const afterIme = (await items("Halo Testtermin")).length;
          t.dispatchEvent(new w.KeyboardEvent("keydown", { key: "Enter", bubbles: true })); await wait(1200);
          return { opened: true, afterIme, created: (await items("Halo Testtermin")).length, closed: p.state };""")
        ok = res["afterIme"] == 0 and res["created"] == 1 and res["closed"] == "closed"
        return ok, f"nach IME-Enter {res['afterIme']}, nach Enter {res['created']} Termin, Fenster {res['closed']}"
    check("Schnellfenster: legt an, IME-Enter nicht", quick_window, results)

    def natural():
        res = m.js('''
          await toCalendar();
          const li = [...d.querySelectorAll('#unifiedToolbarContent li[item-id="new-event"]')].find(l => l.getBoundingClientRect().width);
          li.querySelector("button").click(); await wait(900);
          const p = d.getElementById("mcQuickPanel"); const t = p.querySelector(".mc-qc-title");
          p.querySelector(".mc-qc-calsel").value = testCalendar().id; p.querySelector(".mc-qc-calsel").dispatchEvent(new w.Event("change"));
          t.value = "Halo NL Essen morgen 12:30 für 90 min"; t.dispatchEvent(new w.Event("input", { bubbles: true })); await wait(300);
          p.querySelector(".mc-qc-primary").click(); await wait(1200);
          const it = (await items("Halo NL Essen"))[0];
          if (!it) return null;
          const s = cal.dtz.dateTimeToJsDate(it.startDate), e = cal.dtz.dateTimeToJsDate(it.endDate), tm2 = new Date(); tm2.setDate(tm2.getDate() + 1);
          return { tomorrow: s.getDate() == tm2.getDate(), start: s.getHours() * 60 + s.getMinutes(), minutes: (e - s) / 60000 };''')
        ok = bool(res) and res["tomorrow"] and res["start"] == 750 and res["minutes"] == 90
        return ok, f"{res}"
    check("Schnellfenster: natürliche Eingabe („morgen 12:30 für 90 min“)", natural, results)

    def emoji():
        res = m.js("""
          const { CalEvent } = ChromeUtils.importESModule("resource:///modules/CalEvent.sys.mjs");
          const e = new CalEvent(); e.title = "Geburtstag Testperson";
          const s = cal.dtz.now(); s.hour = 9; s.minute = 0; const en = s.clone(); en.hour = 10;
          e.startDate = s; e.endDate = en; await testCalendar().addItem(e);
          await toCalendar(); w.switchToView("week");
          const label = await until(() => [...d.querySelectorAll("#view-box .event-name-label")].find(l => /Geburtstag Testperson/.test(l.textContent) && l.hasAttribute("data-mc-emoji")));
          const builtin = label?.getAttribute("data-mc-emoji") || null;
          // eigenes Stichwort aus den Einstellungen wirkt sofort und hat Vorrang
          Services.prefs.setStringPref("extensions.mailcards.emojiCustom", "Testperson = ⚽");
          await until(() => label?.getAttribute("data-mc-emoji") == "⚽", 3000);
          const custom = label?.getAttribute("data-mc-emoji") || null;
          Services.prefs.clearUserPref("extensions.mailcards.emojiCustom");
          await until(() => label?.getAttribute("data-mc-emoji") == "🎂", 3000);
          return [builtin, custom, label?.getAttribute("data-mc-emoji") || null];""")
        return res == ["🎂", "⚽", "🎂"], f"eingebaut {res[0]}, eigenes Stichwort {res[1]}, wieder {res[2]}"
    check("Emoji vor Termintitel", emoji, results)

    def readstatus():
        res = m.js("""
          await toCalendar(); const c = testCalendar(); c.readOnly = true; await wait(800);
          const li = [...d.querySelectorAll("#calendar-list li")].find(l => /Halo Test/.test(l.textContent));
          const shown = getComputedStyle(li.querySelector(".calendar-readstatus")).opacity;
          c.readOnly = false; await wait(400); const hidden = getComputedStyle(li.querySelector(".calendar-readstatus")).opacity;
          return [shown, hidden];""")
        return float(res[0]) > 0 and float(res[1]) == 0, f"schreibgeschützt {res[0]}, normal {res[1]}"
    check("Kalenderliste zeigt Status bei Schreibschutz", readstatus, results)

    def splitter():
        pos = lambda: m.js("""const a3 = await toMail(); const off = a3.browsingContext.embedderElement.getBoundingClientRect();
          const s = a3.document.getElementById("folderPaneSplitter").getBoundingClientRect();
          return [Math.round(off.left + s.left + s.width / 2), Math.round(off.top + s.top + 300), Math.round(a3.document.getElementById("folderPane").getBoundingClientRect().width)];""")
        x, y, before = pos()
        m.pointer([{"type": "pointerMove", "x": x, "y": y, "origin": "viewport"}, {"type": "pointerDown", "button": 0}]
                  + [{"type": "pointerMove", "x": x + 5 * i, "y": y, "origin": "viewport", "duration": 20} for i in range(1, 9)]
                  + [{"type": "pointerUp", "button": 0}])
        time.sleep(1.2)
        x, y, dragged = pos()
        m.pointer([{"type": "pointerMove", "x": x, "y": y, "origin": "viewport"}, {"type": "pointerDown", "button": 0}, {"type": "pointerUp", "button": 0},
                   {"type": "pause", "duration": 80}, {"type": "pointerDown", "button": 0}, {"type": "pointerUp", "button": 0}])
        time.sleep(1.2)
        reset = pos()[2]
        return dragged > before and reset == 206, f"{before} → gezogen {dragged} → Doppelklick {reset} (soll 206)"
    check("Ordnerleiste: Ziehen und Doppelklick", splitter, results)

    def double_click():
        x, y = m.js('''
          for (const x of [...Services.wm.getEnumerator(null)]) if (!x.location.href.includes("messenger.xhtml")) x.close();
          await toCalendar(); w.switchToView("week"); await wait(1200);
          const col = [...d.querySelectorAll("#week-view calendar-event-column")].filter(c => c.getBoundingClientRect().width)[1];
          const lbl = [...d.querySelectorAll("#week-view .multiday-timebar-time")].find(l => /^\s*(14[:.]00|0?2:00\s*PM)\s*$/i.test(l.textContent));
          lbl.scrollIntoView({ block: "center" }); await wait(500);
          const r = col.getBoundingClientRect(); return [Math.round(r.left + r.width / 2), Math.round(lbl.getBoundingClientRect().top + 8)];''')
        time.sleep(1.2)
        m.pointer([{"type": "pointerMove", "x": x, "y": y, "origin": "viewport"}, {"type": "pointerDown", "button": 0}, {"type": "pointerUp", "button": 0},
                   {"type": "pause", "duration": 80}, {"type": "pointerDown", "button": 0}, {"type": "pointerUp", "button": 0}])
        res = m.js('''const p = await until(() => { const q = d.getElementById("mcQuickPanel"); return q?.state == "open" && q; }) || d.getElementById("mcQuickPanel");
          const r = { state: p?.state, line: p?.querySelector(".mc-qc-line1").textContent,
            dialogs: [...Services.wm.getEnumerator(null)].filter(x => /calendar-event-dialog/.test(x.location.href)).length };
          p?.hidePopup(); return r;''')
        # „showing“ = geht gerade auf (in einem verdeckten Fenster bleibt die Animation dort stehen)
        ok = res["state"] in ("open", "showing") and res["dialogs"] == 0 and ("14:" in res["line"] or "02:00 PM" in res["line"] or "2:00 PM" in res["line"])
        return ok, f"Fenster {res['state']}, {res['line']}, große Dialoge {res['dialogs']}"
    check("Doppelklick im Kalender öffnet das Schnellfenster", double_click, results)

    def hover_actions():
        res = m.js('''
          const { MailServices } = ChromeUtils.importESModule("resource:///modules/MailServices.sys.mjs");
          try { MailServices.accounts.localFoldersServer; } catch { MailServices.accounts.createLocalMailAccount(); await wait(500); }
          const root = MailServices.accounts.localFoldersServer.rootFolder;
          if (!root.containsChildNamed("Inbox")) { root.createSubfolder("Inbox", null); await wait(800); }
          const inbox = root.getChildNamed("Inbox"); const lf = inbox.QueryInterface(Ci.nsIMsgLocalMailFolder);
          for (const n of [1, 2]) lf.addMessage(`From - Sat Oct 17 10:00:00 2026\\r\\nFrom: Test ${n} <t${n}@example.invalid>\\r\\nSubject: Halo Karte ${n}\\r\\nDate: Sat, 17 Oct 2026 10:0${n}:00 +0200\\r\\nMessage-ID: <halo-card-${n}@example.invalid>\\r\\n\\r\\nTest\\r\\n`);
          const a3 = await toMail(); a3.displayFolder(inbox.URI);
          await until(() => a3.document.querySelectorAll('#threadTree tr[is="thread-card"] .mc-act-flag').length >= 2, 15000);
          const rows = a3.document.querySelectorAll('#threadTree tr[is="thread-card"]');
          if (!rows[0]?.querySelector(".mc-act-flag")) {
            return { diag: true, rows: rows.length, avatars: a3.document.querySelectorAll('#threadTree tr[is="thread-card"] .mc-avatar').length,
                     patched: !!a3.document.__mcPatched,
                     protoOurs: String(a3.customElements.get("thread-card")?.prototype.fillRow).includes("decorate"),
                     rowOwn: rows[0] ? Object.prototype.hasOwnProperty.call(rows[0], "fillRow") : null,
                     rowIsCard: rows[0] instanceof a3.customElements.get("thread-card"),
                     container: !!rows[0]?.querySelector(".card-container"),
                     sameDoc: rows[0]?.ownerDocument == a3.document, tableRows: a3.document.querySelectorAll('#threadTree tr[is="thread-row"]').length,
                     pref: Services.prefs.getBoolPref("extensions.mailcards.hoverActions", false), msgs: inbox.getTotalMessages(false),
                     folder: a3.gFolder?.URI == inbox.URI, view: Services.prefs.getIntPref("mail.threadpane.listview", -1) };
          }
          const buttons = rows[0]?.querySelectorAll(".mc-action") || [];
          rows[0].querySelector(".mc-act-flag").click(); await wait(500);
          const flagged = a3.gDBView.getMsgHdrAt(0).isFlagged; const selected = a3.gDBView.selection.count;
          rows[0].querySelector(".mc-act-delete").click(); await wait(1200);
          const trash = root.getFolderWithFlags(Ci.nsMsgFolderFlags.Trash);
          return { buttons: buttons.length, flagged, selected, inbox: inbox.getTotalMessages(false), trash: trash?.getTotalMessages(false) };''')
        if res.get("diag"):
            return False, f"keine Knöpfe auf der Karte – Diagnose {res}"
        ok = res["buttons"] == 3 and res["flagged"] and res["selected"] == 0 and res["inbox"] == 1 and res["trash"] == 1
        return ok, f"{res}"
    check("Mailkarte: Aktionen beim Überfahren (Markieren, Löschen)", hover_actions, results)

    def selected_actions():
        res = m.js('''
          const a3 = await toMail(); a3.threadTree.selectedIndex = 0; a3.threadTree.focus();
          await until(() => a3.document.querySelector('#threadTree tr[is="thread-card"][aria-selected="true"] .mc-action, #threadTree tr[is="thread-card"].selected .mc-action'));
          const row = a3.document.querySelector('#threadTree tr[is="thread-card"].selected, #threadTree tr[is="thread-card"][aria-selected="true"]');
          const b = row?.querySelector(".mc-action"); const box = row?.querySelector(".mc-actions");
          if (!b) return { found: false };
          // rgb(255, 255, 255) und color(srgb 1 1 1) auf dieselbe Skala 0–255
          const rgb = c => { const v = (c.match(/[\\d.]+/g) || []).slice(0, 3).map(Number); return c.startsWith("color(") ? v.map(x => x * 255) : v; };
          const cs = a3.getComputedStyle; const fg = rgb(cs(b).color), bg = rgb(cs(box).backgroundColor);
          const diff = Math.max(...fg.map((v, i) => Math.abs(v - bg[i])));
          return { found: true, icon: cs(b).color, pill: cs(box).backgroundColor, diff };''')
        return res.get("found") and res["diff"] >= 60, f"{res}"
    check("Ausgewählte Karte: Aktions-Symbole sichtbar", selected_actions, results)

    def reading():
        res = m.js(MESSAGE_JS + '''
          const am = a3.messageBrowser.contentWindow; const md = am.document;
          const box = await until(() => md.querySelector("#fromRecipient0 .recipient-avatar.mc-hdr-avatar"));
          const body = md.getElementById("messagepane")?.contentDocument;
          const pre = await until(() => body?.querySelector(".moz-text-plain pre, .moz-text-flowed, div.moz-text-plain"));
          const font = pre ? body.defaultView.getComputedStyle(pre).fontFamily : "";
          const title = a3.document.getElementById("threadPaneFolderName")?.textContent || "";
          const head = [...a3.document.querySelectorAll("#threadPaneHeaderBar *")].map(e => e.textContent).join(" ");
          const vis = e => !!e && e.getBoundingClientRect().height > 0;
          const banner = md.getElementById("mcListBanner");
          return { avatar: box?.textContent.trim(), size: box ? Math.round(box.getBoundingClientRect().width) : 0, font, title, undefinedInHeader: /undefined|null/.test(head),
                   listIdRow: vis(md.getElementById("expandedlist-id")), banner: vis(banner), unsub: !!banner?.querySelector(".mc-lb-unsub"),
                   folder: md.querySelector("#mcHdrMeta .mc-hdr-folder")?.textContent, date: md.querySelector("#mcHdrMeta #dateLabel")?.textContent };''')
        ok = (res["avatar"] == "AB" and res["size"] >= 36 and "monospace" not in res["font"].lower() and not res["undefinedInHeader"]
              and not res["listIdRow"] and res["banner"] and res["unsub"] and "Lesen" in (res["folder"] or "") and res["date"])
        return ok, f"{res}"
    check("Leseansicht wie Apple: Absenderbild, Ordner, Verteiler-Hinweis statt List-ID", reading, results)

    def unsubscribe():
        res = m.js(MESSAGE_JS + '''
          const am = a3.messageBrowser.contentWindow; const md = am.document;
          const btn = await until(() => md.querySelector("#mcListBanner .mc-lb-unsub"));
          // nicht wirklich den Browser öffnen: Aufruf abfangen
          const orig = am.openLinkExternally; let opened = null;
          am.openLinkExternally = url => { opened = String(url); };
          btn?.click(); await wait(300);
          am.openLinkExternally = orig;
          const log = await IOUtils.readUTF8(PathUtils.join(PathUtils.profileDir, "mail-cards-debug.log")).catch(() => "");
          return { button: !!btn, opened, error: /unsubscribe error/.test(log) };''')
        return res["button"] and res["opened"] == "https://example.invalid/abmelden?id=1" and not res["error"], f"{res}"
    check("Verteiler-Hinweis: Abmelden öffnet den Link", unsubscribe, results)

    def html_font():
        res = m.js(r"""
          const { MailServices } = ChromeUtils.importESModule("resource:///modules/MailServices.sys.mjs");
          try { MailServices.accounts.localFoldersServer; } catch { MailServices.accounts.createLocalMailAccount(); await wait(500); }
          const root = MailServices.accounts.localFoldersServer.rootFolder;
          if (!root.containsChildNamed("Schrift")) { root.createSubfolder("Schrift", null); await wait(800); }
          const f = root.getChildNamed("Schrift"); const lf = f.QueryInterface(Ci.nsIMsgLocalMailFolder);
          const mail = (n, body) => `From: Halo <h@example.invalid>\r\nSubject: Schrift ${n}\r\nDate: Sat, 17 Oct 2026 10:0${n}:00 +0200\r\nMessage-ID: <halo-font-${n}@example.invalid>\r\nMIME-Version: 1.0\r\nContent-Type: text/html; charset=UTF-8\r\n\r\n${body}\r\n\r\n`;
          if (!f.getTotalMessages(false)) {
            lf.addMessage(mail(1, "<html><body><p id='t'>Ohne eigene Schrift</p><img src='https://example.invalid/bild.png' width='1' height='1'></body></html>"));
            lf.addMessage(mail(2, "<html><body style='font-family: Georgia, serif'><p id='t'>Mit eigener Schrift</p></body></html>"));
            await wait(800);
          }
          const a3 = await toMail(); a3.displayFolder(f.URI); await wait(800);
          const out = {};
          for (const i of [0, 1]) {
            a3.threadTree.selectedIndex = i; await wait(1500);
            const md = a3.messageBrowser.contentDocument;
            const body = await until(() => md.getElementById("messagepane")?.contentDocument?.getElementById("t"));
            const subj = a3.gDBView.getMsgHdrAt(i).subject;
            out[subj] = body ? body.ownerDocument.defaultView.getComputedStyle(body).fontFamily : null;
            out.pill = md.getElementById("header-view-toolbox")?.getBoundingClientRect().width || 0;
            out.dates = [...md.querySelectorAll("#dateLabel, #dateLabelSubject")].filter(e => e.getBoundingClientRect().width).length;
          }
          return out;""")
        own, plain = res.get("Schrift 2") or "", res.get("Schrift 1") or ""
        ok = "apple-system" in plain and own.startswith("Georgia") and not res["pill"] and res["dates"] == 1
        return ok, f"{res}"
    check("Mails in Systemschrift, eigene Schrift bleibt, keine Kapsel im Kopf", html_font, results)

    def remote_bar():
        res = m.js(r"""
          const { MailServices } = ChromeUtils.importESModule("resource:///modules/MailServices.sys.mjs");
          const f = MailServices.accounts.localFoldersServer.rootFolder.getChildNamed("Schrift");
          const a3 = await toMail(); a3.displayFolder(f.URI); await wait(800);
          for (let i = 0; i < a3.gDBView.rowCount; i++) if (a3.gDBView.getMsgHdrAt(i).subject == "Schrift 1") a3.threadTree.selectedIndex = i;
          const md = a3.messageBrowser.contentDocument; const mw = md.defaultView;
          const n = await until(() => md.querySelector('#mail-notification-top notification-message[value="remoteContent"]'));
          await until(() => n?.shadowRoot?.querySelector("style.mc-layout"), 3000); await wait(600);
          const text = n?.shadowRoot?.querySelector(".text-container")?.getBoundingClientRect();
          const btn = n?.querySelector("button")?.getBoundingClientRect();
          return { bar: !!n, styled: !!n?.shadowRoot?.querySelector("style.mc-layout"), paneWidth: Math.round(md.documentElement.clientWidth),
                   sameRow: !!(text && btn && btn.top < text.bottom && btn.left >= text.right - 1), bg: n && mw.getComputedStyle(n).backgroundColor };""")
        yellow = res.get("bg", "").startswith("rgb(255, 2")
        return res.get("bar") and res.get("styled") and res.get("sameRow") and yellow, f"{res}"
    check("Hinweis über der Mail: Knopf neben dem Text, Farbe unverändert", remote_bar, results)

    def min_widths():
        res = m.js('''
          const out = {};
          const probe = async (name, open, getEl) => {
            await open(); w.resizeTo(1500, 900); await wait(1500);
            w.resizeTo(600, 900); await wait(1500);
            const el = getEl(); const r = el?.getBoundingClientRect();
            out[name] = { window: w.outerWidth, pane: r ? Math.round(r.width) : null, min: el ? parseFloat(el.ownerDocument.defaultView.getComputedStyle(el).minWidth) : null };
          };
          await probe("mail", () => toMail(), () => tm.currentAbout3Pane.document.getElementById("messagePane"));
          await probe("calendar", () => toCalendar(), () => d.getElementById("calendarDisplayBox"));
          await probe("settings", async () => { w.openPreferencesTab(); await wait(2000); }, () => tm.currentTabInfo.browser.contentDocument.getElementById("preferencesContainer"));
          w.resizeTo(1500, 900); await wait(800); await toMail();
          return out;''')
        ok = all(v["pane"] is not None and v["pane"] >= v["min"] - 1 and v["window"] > 600 for v in res.values())
        return ok, f"{res}"
    check("Mindestbreiten: Fenster wird nicht schmaler als die Bereiche", min_widths, results)

    def today_animation():
        res = m.js('''
          await toMail(); await wait(1500);
          const panel = d.getElementById("today-pane-panel"); const btn = d.getElementById("mcTodayToggle");
          const record = async () => { const ws = [], ts = []; const t0 = performance.now(); while (performance.now() - t0 < 600) { ws.push(Math.round(panel.getBoundingClientRect().width)); ts.push(performance.now()); await new Promise(r => w.requestAnimationFrame(r)); } return { ws, ts }; };
          const out = { start: Math.round(panel.getBoundingClientRect().width) };
          if (!out.start) return { skipped: true };
          for (const phase of ["aus", "ein"]) {
            btn.click(); const { ws, ts } = await record(); await wait(300);
            // nur Bilder, in denen sich die Breite ändert (die Animation selbst)
            const moving = ws.map((v, i) => i > 0 && v != ws[i - 1]);
            const dts = ts.slice(1).map((t, i) => t - ts[i]).filter((_, i) => moving[i + 1]);
            const jumps = ws.slice(1).map((v, i) => Math.abs(v - ws[i]));
            out[phase] = { steps: moving.filter(Boolean).length + 1, maxJump: Math.max(...jumps), maxFrameMs: Math.round(Math.max(...dts)), medianFrameMs: Math.round(dts.sort((a, b) => a - b)[dts.length >> 1] || 0) };
          }
          out.end = Math.round(panel.getBoundingClientRect().width);
          return out;''')
        if res.get("skipped"):
            return True, "Today-Pane zu – übersprungen"
        # flüssig: viele Zwischenschritte, kein Sprung über die halbe Breite
        ok = all(res[p]["steps"] >= 6 and res[p]["maxJump"] < res["start"] / 2 for p in ("aus", "ein")) and res["end"] == res["start"]
        return ok, f"{res}"
    check("Today-Pane: Ein-/Ausblenden flüssig (kein Sprung)", today_animation, results)

    def birthdays():
        res = m.js('''
          const { MailServices } = ChromeUtils.importESModule("resource:///modules/MailServices.sys.mjs");
          const { VCardUtils } = ChromeUtils.importESModule("resource:///modules/VCardUtils.sys.mjs");
          const book = MailServices.ab.getDirectory("jsaddrbook://abook.sqlite");
          const card = VCardUtils.vCardToAbCard("BEGIN:VCARD\\r\\nVERSION:4.0\\r\\nFN:Halo Testkontakt\\r\\nBDAY:1990-03-15\\r\\nEND:VCARD\\r\\n");
          book.addCard(card);
          // der Kalender wird 3 s nach der Änderung neu gefüllt
          let c = null;
          for (let i = 0; i < 30 && !c; i++) { await wait(500); c = cal.manager.getCalendarById(Services.prefs.getStringPref("extensions.mailcards.birthdayCalendarId", "")); }
          if (!c) return { calendar: false };
          await wait(800);
          const all = await c.getItemsAsArray(Ci.calICalendar.ITEM_FILTER_ALL_ITEMS, 0, null, null);
          const it = all.find(i => /Halo Testkontakt/.test(i.title));
          return { calendar: true, name: c.name, readOnly: c.readOnly, title: it?.title, yearly: it?.recurrenceInfo?.getRecurrenceItems()[0]?.type, allDay: it?.startDate.isDate };''')
        ok = res.get("calendar") and res.get("readOnly") and res.get("yearly") == "YEARLY" and res.get("allDay") and "Halo Testkontakt" in (res.get("title") or "")
        return ok, f"{res}"
    check("Geburtstage aus dem Adressbuch als Kalender", birthdays, results)

    def keyboard():
        res = m.js('''
          await toMail(); await wait(300);
          const ring = el => { el.focus({ focusVisible: true }); const cs = getComputedStyle(el); const r = cs.outlineStyle != "none" && parseFloat(cs.outlineWidth) >= 2; el.blur(); return r; };
          const own = ["mcSpacesToggle", "mcTodayToggle"].map(id => d.getElementById(id)).filter(Boolean);
          const unlabeled = own.filter(b => !b.getAttribute("aria-label")).map(b => b.id);
          const rings = own.filter(b => !ring(b)).map(b => b.id);
          const a3 = d.getElementById("tabmail").currentAbout3Pane;
          const acts = [...(a3?.document.querySelectorAll(".mc-action") || [])];
          return { own: own.length, unlabeled, noRing: rings, actions: acts.length, tabbable: acts.filter(b => b.tabIndex >= 0).length,
                   actionsLabeled: acts.every(b => b.getAttribute("aria-label")) };''')
        ok = res["own"] == 2 and not res["unlabeled"] and not res["noRing"] and res["tabbable"] == 0 and res["actionsLabeled"]
        return ok, f"{res}"
    check("Tastatur: Fokusring und Beschriftung eigener Knöpfe", keyboard, results)

    def auto_update():
        res = m.js('''
          const last = await until(() => Services.prefs.getIntPref("extensions.mailcards.autoUpdateLast", 0), 25000);
          return { last, ageSec: last ? Math.round(Date.now() / 1000 - last) : null };''')
        return bool(res["last"]) and res["ageSec"] < 600, f"Update-Prüfung beim Start gelaufen: {res}"
    check("Updates: Prüfung beim Start", auto_update, results)

    def addons_and_menubar():
        res = m.js('''
          w.openAddonsMgr("addons://list/extension"); await wait(2500);
          const doc = tm.currentTabInfo.browser.contentDocument; const sb = doc.getElementById("sidebar"), content = doc.getElementById("content");
          // breite gemeinsame Seitenleiste (wie von der Erweiterung gesetzt) und ausgeblendete Leiste
          sb.style.setProperty("width", "406px", "important"); await wait(300);
          const wide = { sidebar: Math.round(sb.getBoundingClientRect().right), content: Math.round(content.getBoundingClientRect().left) };
          sb.classList.add("mc-side-hidden"); await wait(300);
          const hidden = Math.round(content.getBoundingClientRect().width), page = Math.round(doc.documentElement.clientWidth);
          sb.classList.remove("mc-side-hidden"); sb.style.removeProperty("width");
          let menubar = "Mac: Menüleiste des Systems";
          if (Services.appinfo.OS != "Darwin") {
            // ausgeblendet (Start: autohide="true") kein Streifen; eingeschaltet ganz oben über der Toolbar-Zeile;
            // wieder aus wie über Ansicht → Symbolleisten (autohide="") ebenfalls kein Streifen
            const mb = d.getElementById("toolbar-menubar"); const off = Math.round(mb.getBoundingClientRect().height);
            mb.removeAttribute("autohide"); await wait(500);
            const r = mb.getBoundingClientRect();
            menubar = { off, h: Math.round(r.height), bottom: Math.round(r.bottom), toolbar: Math.round(d.getElementById("unifiedToolbarContainer").getBoundingClientRect().top) };
            mb.setAttribute("autohide", ""); await wait(500);
            menubar.toggledOff = Math.round(mb.getBoundingClientRect().height);
            mb.setAttribute("autohide", "true");
          }
          tm.closeTab(tm.currentTabInfo); await toMail();
          return { wide, hidden, page, menubar };''')
        ok = (res["wide"]["content"] >= res["wide"]["sidebar"] - 1 and res["hidden"] >= res["page"] - 1
              and (isinstance(res["menubar"], str) or (res["menubar"]["off"] == 0 and res["menubar"]["toggledOff"] == 0 and res["menubar"]["h"] >= 15
                                                       and res["menubar"]["bottom"] <= res["menubar"]["toolbar"])))
        return ok, f"{res}"
    check("Add-ons: Inhalt neben breiter/ausgeblendeter Leiste; Menüleiste über der Toolbar", addons_and_menubar, results)

    def errors():
        res = m.js("""
          const p = PathUtils.join(PathUtils.profileDir, "mail-cards-debug.log");
          let lines = []; try { lines = (await IOUtils.readUTF8(p)).split("\\n"); } catch {}
          return lines.filter(l => /error|fehler/i.test(l) && !/preview stats/.test(l)).slice(0, 5);""")
        return not res, "; ".join(res) or "keine Fehler im Protokoll"
    check("Fehlerprotokoll der Erweiterung leer", errors, results)


if __name__ == "__main__":
    main()
