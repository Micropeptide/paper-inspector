import { useEffect, useMemo, useRef, useState } from "react";
import { api } from "./api";
import Home from "./Home";
import { readStore, useHashRoute, useKeydown, writeStore } from "./lib/hooks";
import { Command, CommandProvider, Palette, ShortcutSheet, useCommands } from "./Palette";
import Queue from "./Queue";
import RunView from "./RunView";
import SettingsDialog from "./Settings";
import { ToastProvider } from "./ui";

const THEMES = ["system", "light", "dark"];

function applyTheme(t: string) {
  if (t === "light" || t === "dark") document.documentElement.dataset.theme = t;
  else delete document.documentElement.dataset.theme;
}

export default function App() {
  const [palette, setPalette] = useState(false);
  return (
    <ToastProvider>
      <CommandProvider render={(items) => palette && <Palette items={items} onClose={() => setPalette(false)} />}>
        <Shell openPalette={() => setPalette(true)} paletteOpen={palette} />
      </CommandProvider>
    </ToastProvider>
  );
}

function Shell({ openPalette, paletteOpen }: { openPalette: () => void; paletteOpen: boolean }) {
  const [route, go] = useHashRoute();
  const [settings, setSettings] = useState(false);
  const [help, setHelp] = useState(false);
  const [theme, setThemeState] = useState<string>(() => readStore("pi.theme", "system"));
  const [version, setVersion] = useState("");

  const setTheme = (t: string) => {
    setThemeState(t);
    writeStore("pi.theme", t);
    applyTheme(t);
  };
  const cycleTheme = () => setTheme(THEMES[(THEMES.indexOf(theme) + 1) % THEMES.length]);

  useEffect(() => {
    api.health().then((h) => setVersion(h.version), () => undefined);
  }, []);

  const modal = settings || help || paletteOpen;
  useKeydown((e) => {
    const mod = e.metaKey || e.ctrlKey;
    if (mod && (e.key === "k" || e.key === "K")) { e.preventDefault(); openPalette(); return; }
    if (mod && e.key === ",") { e.preventDefault(); setSettings(true); return; }
    if (!mod && e.key === "?") { e.preventDefault(); setHelp(true); }
  }, !modal);

  const commands = useMemo<Command[]>(() => [
    { id: "nav-home", group: "Go to", label: "All scans", keys: "g h", run: () => go({ page: "home" }) },
    { id: "nav-queue", group: "Go to", label: "Batch queue", keys: "g q", run: () => go({ page: "queue" }) },
    { id: "app-settings", group: "Actions", label: "Settings", keys: "Cmd ,", run: () => setSettings(true) },
    { id: "app-theme", group: "Actions", label: `Change theme (now ${theme})`, run: cycleTheme },
    { id: "app-help", group: "Actions", label: "Keyboard shortcuts", keys: "?", run: () => setHelp(true) },
    // eslint-disable-next-line react-hooks/exhaustive-deps
  ], [go, theme]);
  useCommands("app", commands);

  const gAt = useRef(0);
  useKeydown((e) => {
    if (route.page === "run" || e.metaKey || e.ctrlKey || e.altKey) return;
    if (e.key === "g") { gAt.current = Date.now(); return; }
    if (Date.now() - gAt.current < 1000) {
      if (e.key === "h") { e.preventDefault(); go({ page: "home" }); }
      if (e.key === "q") { e.preventDefault(); go({ page: "queue" }); }
      gAt.current = 0;
    }
  }, !modal);

  return (
    <>
      {route.page === "run" && route.run ? (
        <RunView key={route.run} id={route.run} route={route} go={go} onSettings={() => setSettings(true)} onShortcuts={() => setHelp(true)} />
      ) : route.page === "queue" ? (
        <Queue batch={route.batch} go={go} />
      ) : (
        <Home go={go} theme={theme} onTheme={cycleTheme} onSettings={() => setSettings(true)} version={version} />
      )}
      {settings && <SettingsDialog onClose={() => setSettings(false)} theme={theme} setTheme={setTheme} />}
      {help && <ShortcutSheet onClose={() => setHelp(false)} />}
    </>
  );
}
