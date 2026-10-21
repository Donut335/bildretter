// Runs the Python repair engine (bildretter/engine.py) in the browser via Pyodide.
importScripts("https://cdn.jsdelivr.net/pyodide/v0.29.5/full/pyodide.js");

const report = (text) => postMessage({ type: "progress", text });
self.report = report;

const ready = (async () => {
  const py = await loadPyodide();
  await py.loadPackage(["numpy", "pillow"]);
  py.FS.mkdirTree("/home/pyodide/bildretter");
  for (const name of ["__init__.py", "jpegscan.py", "engine.py"]) {
    const res = await fetch(`py/${name}`, { cache: "no-cache" });  // always the current engine
    if (!res.ok) throw new Error(`py/${name} nicht gefunden`);
    py.FS.writeFile(`/home/pyodide/bildretter/${name}`, await res.text());
  }
  await py.runPythonAsync(`
import sys, io, zipfile, shutil
from pathlib import Path
sys.path.insert(0, "/home/pyodide")
from bildretter.engine import process_files

def run_repair(names):
    import js
    out = Path("/tmp/out")
    shutil.rmtree(out, ignore_errors=True)
    results = process_files([Path("/tmp/in") / n for n in names], out, js.report)
    buf = io.BytesIO()
    ok = [r for r in results if r.ok and r.output]
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:  # PNG/JPEG are already compressed
        for r in ok:
            z.write(r.output, "Gerettete Bilder/" + r.output.name)
    return [{"name": r.name, "ok": r.ok, "message": r.message, "combined": bool(r.extra.get("combined"))}
            for r in results], (buf.getvalue() if ok else None)
`);
  return py;
})();

self.onmessage = async ({ data }) => {
  if (data.type !== "repair") return;
  try {
    report("Lade Reparatur-Programm (beim ersten Mal ca. 20 MB) …");
    const py = await ready;
    py.FS.mkdirTree("/tmp/in");
    for (const f of py.FS.readdir("/tmp/in")) if (f !== "." && f !== "..") py.FS.unlink(`/tmp/in/${f}`);
    const names = [];
    for (const f of data.files) {
      const name = f.name.replace(/[\\/]/g, "_");
      py.FS.writeFile(`/tmp/in/${name}`, new Uint8Array(f.data));
      names.push(name);
    }
    // let the progress message reach the page before the heavy work blocks this thread
    await new Promise((r) => setTimeout(r, 0));
    const res = py.globals.get("run_repair")(py.toPy(names));
    const [results, zip] = res.toJs({ dict_converter: Object.fromEntries });
    res.destroy();
    const zipBytes = zip ? zip.slice() : null;
    postMessage({ type: "done", results, zip: zipBytes }, zipBytes ? [zipBytes.buffer] : []);
  } catch (e) {
    postMessage({ type: "error", text: String(e && e.message || e) });
  }
};
