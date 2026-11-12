// Packs one STEP file into a zip inside the browser, so only the compressed copy is uploaded.
//
// The zip is built by hand (a few dozen lines of the standard zip layout) because the page may not load
// libraries from other sites: the Content-Security-Policy only allows scripts from this site. Compression uses
// the browser's own CompressionStream; where that is missing the file is stored without compression, which is
// still a valid zip. The server checks every upload again, so nothing here is trusted for safety.
(() => {
  "use strict";

  const STEP_MAGIC = "ISO-10303-21";
  const MAX_CLASSIC_ZIP = 0xFFFFFFFF;       // without the zip64 extension, sizes must fit in 32 bits

  // ---- CRC-32 (the checksum every zip entry carries) ------------------------------------------------

  const TABLE = (() => {
    const t = new Uint32Array(256);
    for (let n = 0; n < 256; n++) {
      let c = n;
      for (let k = 0; k < 8; k++) c = c & 1 ? 0xEDB88320 ^ (c >>> 1) : c >>> 1;
      t[n] = c >>> 0;
    }
    return t;
  })();

  // A running CRC-32: call update() with each chunk in order, then read value.
  class Crc32 {
    constructor() { this.state = 0xFFFFFFFF; }
    update(bytes) {
      let c = this.state;
      for (let i = 0; i < bytes.length; i++) c = TABLE[(c ^ bytes[i]) & 0xFF] ^ (c >>> 8);
      this.state = c;
    }
    get value() { return (this.state ^ 0xFFFFFFFF) >>> 0; }
  }

  // ---- names and checks ------------------------------------------------------------------------------

  // The name stored inside the zip: plain letters, digits and . _ - only, ending in .step or .stp.
  // The real file name is kept separately for display; this one never needs to match it.
  function entryName(filename) {
    const base = String(filename || "").split(/[\\/]/).pop();
    const dot = base.lastIndexOf(".");
    const ext = dot >= 0 ? base.slice(dot + 1).toLowerCase() : "";
    const stem = (dot >= 0 ? base.slice(0, dot) : base).replace(/[^A-Za-z0-9._-]+/g, "_").replace(/^[._]+/, "");
    return (stem.slice(0, 70) || "wing") + "." + (ext === "stp" ? "stp" : "step");
  }

  // True if the file starts like a STEP file (after an optional byte-order mark and spaces).
  async function looksLikeStep(file) {
    const head = new Uint8Array(await file.slice(0, 1024).arrayBuffer());
    const text = new TextDecoder("latin1").decode(head).replace(/^[﻿\xEF\xBB\xBF \t\r\n]+/, "");
    return text.startsWith(STEP_MAGIC);
  }

  // ---- building the zip ------------------------------------------------------------------------------

  // MS-DOS date and time, as zip stores them (two-second steps, years from 1980).
  function dosStamp(ms) {
    const d = new Date(Number.isFinite(ms) ? ms : Date.now());
    const year = Math.min(Math.max(d.getFullYear(), 1980), 2107);
    return {
      time: (d.getHours() << 11) | (d.getMinutes() << 5) | (d.getSeconds() >> 1),
      date: ((year - 1980) << 9) | ((d.getMonth() + 1) << 5) | d.getDate(),
    };
  }

  // The three fixed-layout records around the data: local header, central directory entry, end record.
  function headers(name, info) {
    const nameBytes = new TextEncoder().encode(name);          // the name is ASCII (see entryName)
    const method = info.deflated ? 8 : 0;
    const need = info.deflated ? 20 : 10;

    const local = new DataView(new ArrayBuffer(30 + nameBytes.length));
    local.setUint32(0, 0x04034b50, true);
    local.setUint16(4, need, true);
    local.setUint16(6, 0, true);
    local.setUint16(8, method, true);
    local.setUint16(10, info.time, true);
    local.setUint16(12, info.date, true);
    local.setUint32(14, info.crc, true);
    local.setUint32(18, info.packed, true);
    local.setUint32(22, info.size, true);
    local.setUint16(26, nameBytes.length, true);
    local.setUint16(28, 0, true);
    new Uint8Array(local.buffer).set(nameBytes, 30);

    const central = new DataView(new ArrayBuffer(46 + nameBytes.length));
    central.setUint32(0, 0x02014b50, true);
    central.setUint16(4, 20, true);
    central.setUint16(6, need, true);
    central.setUint16(8, 0, true);
    central.setUint16(10, method, true);
    central.setUint16(12, info.time, true);
    central.setUint16(14, info.date, true);
    central.setUint32(16, info.crc, true);
    central.setUint32(20, info.packed, true);
    central.setUint32(24, info.size, true);
    central.setUint16(28, nameBytes.length, true);
    central.setUint32(42, 0, true);                             // the data starts at offset 0 in the file
    new Uint8Array(central.buffer).set(nameBytes, 46);

    const end = new DataView(new ArrayBuffer(22));
    end.setUint32(0, 0x06054b50, true);
    end.setUint16(8, 1, true);
    end.setUint16(10, 1, true);
    end.setUint32(12, central.byteLength, true);
    end.setUint32(16, local.byteLength + info.packed, true);    // where the central directory starts
    return [local, central, end].map((v) => new Uint8Array(v.buffer));
  }

  // Read a file once, in order, giving each chunk to `each` (used to checksum a file that is stored as it is).
  async function readAll(file, each) {
    const reader = file.stream().getReader();
    for (;;) {
      const { done, value } = await reader.read();
      if (done) return;
      each(value);
    }
  }

  // Zip one file. Resolves to a zip Blob holding that file under `name`.
  // onProgress(fraction) is called while the file is read; set `deflate: false` to skip compression.
  async function zipFile(file, name, onProgress, deflate = true) {
    if (file.size > MAX_CLASSIC_ZIP) throw new Error("That file is too large to zip in the browser.");
    if (typeof file.stream !== "function") throw new Error("This browser cannot read files in chunks. Use a current one.");
    const stamp = dosStamp(file.lastModified);
    const crc = new Crc32();
    let size = 0;
    const tap = (chunk) => {
      crc.update(chunk);
      size += chunk.length;
      if (onProgress) onProgress(file.size ? Math.min(size / file.size, 1) : 1);
    };
    const canDeflate = deflate && typeof CompressionStream === "function";
    let data;
    let packed;
    if (canDeflate) {
      // Checksum and compress in one pass over the file; the compressed pieces are kept in memory.
      const tapper = new TransformStream({ transform(chunk, controller) { tap(chunk); controller.enqueue(chunk); } });
      const reader = file.stream().pipeThrough(tapper).pipeThrough(new CompressionStream("deflate-raw")).getReader();
      data = [];
      packed = 0;
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        data.push(value);
        packed += value.length;
      }
    } else {
      await readAll(file, tap);
      data = [file];
      packed = file.size;
    }
    if (packed > MAX_CLASSIC_ZIP) throw new Error("That file is too large to zip in the browser.");
    const [local, central, end] = headers(name, { deflated: canDeflate, packed, size, crc: crc.value, ...stamp });
    return new Blob([local, ...data, central, end], { type: "application/zip" });
  }

  const api = { Crc32, entryName, looksLikeStep, zipFile };
  if (typeof module !== "undefined" && module.exports) module.exports = api;       // for the tests (Node)
  else window.CFD_ZIP = api;
})();
