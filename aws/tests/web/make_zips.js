// Builds test zips with web/zip.js and writes them to the folder given as the first argument.
// tests/test_web_zip.py then opens them with Python's own zip reader and the server's STEP check.
const fs = require("fs");
const path = require("path");
const assert = require("assert");
const zip = require("../../web/zip.js");

const out = process.argv[2];

// A realistic-looking STEP body: structured text lines with varying numbers (compresses about 5-10x, not 1000x).
function stepText(lines, seed = 1) {
  let s = seed >>> 0;
  const rnd = () => ((s = (Math.imul(s, 1664525) + 1013904223) >>> 0) / 4294967296);
  const rows = ["ISO-10303-21;", "HEADER;", "FILE_DESCRIPTION(('test wing'),'2;1');", "ENDSEC;", "DATA;"];
  for (let i = 1; i <= lines; i++) {
    rows.push(`#${i}=CARTESIAN_POINT('',(${(rnd() * 900).toFixed(6)},${(rnd() * 900).toFixed(6)},${(rnd() * 900).toFixed(6)}));`);
  }
  rows.push("ENDSEC;", "END-ISO-10303-21;", "");
  return rows.join("\n");
}

(async () => {
  // name checks
  assert.strictEqual(zip.entryName("Front Wing v3.STEP"), "Front_Wing_v3.step");
  assert.strictEqual(zip.entryName("..\\..\\evil/../a b.stp"), "a_b.stp");
  assert.strictEqual(zip.entryName("wing"), "wing.step");
  assert.strictEqual(zip.entryName("wing.txt"), "wing.step");
  assert.strictEqual(zip.entryName("héllo wörld.step"), "h_llo_w_rld.step");
  assert.strictEqual(zip.entryName(""), "wing.step");
  assert.ok(zip.entryName("a".repeat(300) + ".step").length <= 75);
  assert.strictEqual(zip.entryName(".hidden.step"), "hidden.step");

  // checksum check against a known value: CRC-32("123456789") = CBF43926
  const crc = new zip.Crc32();
  crc.update(new TextEncoder().encode("12345"));
  crc.update(new TextEncoder().encode("6789"));
  assert.strictEqual(crc.value.toString(16), "cbf43926");

  // header check
  const mk = (text, name = "x.step") => new File([text], name, { lastModified: Date.UTC(2026, 10, 12, 9, 30, 0) });
  assert.ok(await zip.looksLikeStep(mk("ISO-10303-21;\nHEADER;")));
  assert.ok(await zip.looksLikeStep(mk("﻿  \r\nISO-10303-21;")));
  assert.ok(!await zip.looksLikeStep(mk("solid stl\nfacet normal")));
  assert.ok(!await zip.looksLikeStep(mk("")));

  const cases = {
    small: [mk(stepText(50, 1)), true],
    medium: [mk(stepText(60000, 2)), true],
    stored: [mk(stepText(2000, 3)), false],
    odd_name: [mk(stepText(10, 4), "Wing (final) v2 ñ.STP"), true],
  };
  const progress = [];
  for (const [label, [file, deflate]] of Object.entries(cases)) {
    const name = zip.entryName(file.name);
    const blob = await zip.zipFile(file, name, label === "medium" ? (f) => progress.push(f) : null, deflate);
    fs.writeFileSync(path.join(out, `${label}.zip`), Buffer.from(await blob.arrayBuffer()));
    fs.writeFileSync(path.join(out, `${label}.step`), Buffer.from(await file.arrayBuffer()));
    fs.writeFileSync(path.join(out, `${label}.name`), name);
  }
  // (Node hands an in-memory file over as one chunk, so only 'ends at 1 and never goes backwards' is checked here.)
  assert.ok(progress.length >= 1 && progress[progress.length - 1] === 1 && progress.every((p, i) => !i || p >= progress[i - 1]));
  console.log("node ok");
})().catch((e) => { console.error(e); process.exit(1); });
