/* Browser & Node.js native PDF object-stream parser (zero third-party dependencies).
 *
 * Performs bounded local metadata inspection including native FlateDecode
 * decompression (ObjStm / XRef / lightweight text streams), per-page box &
 * orientation analysis, measured bleed calculation, and bitmap resolution clues.
 * Results are inspection clues only and never replace professional prepress sign-off.
 */

const PT_TO_MM = 25.4 / 72;
const SIZE_TOLERANCE_MM = 1.5;

const MAX_DECOMPRESSED_STREAMS = 64;
const MAX_OBJSTM_INPUT_BYTES = 1024 * 1024;
const MAX_GENERIC_STREAM_INPUT_BYTES = 256 * 1024;
const MAX_STREAM_OUTPUT_BYTES = 2 * 1024 * 1024;
const MAX_TOTAL_OUTPUT_BYTES = 8 * 1024 * 1024;
const MAX_VARIANTS = 6;
const MAX_SAMPLE_PAGES = 5;
const MAX_MISMATCH_PAGES = 8;
const MAX_LOW_RES_SAMPLES = 4;
const MAX_JSON_BYTES = 7600;

const UNSUPPORTED_FILTERS = [
  "DCTDecode",
  "JPXDecode",
  "JBIG2Decode",
  "CCITTFaxDecode",
  "LZWDecode",
  "ASCIIHexDecode",
  "ASCII85Decode",
  "RunLengthDecode",
  "Crypt",
];

function createFallbackInspection() {
  return {
    readable: false,
    isPdf: false,
    pdfVersion: null,
    pageCount: null,
    encrypted: false,
    hasEof: false,
    boxes: { media: null, crop: null, trim: null, bleed: null },
    colorSpaces: [],
    fontEmbedding: "unknown",
    imageCount: 0,
    hasTransparency: false,
    hasOverprint: false,
    compressedStreams: { detected: 0, decoded: 0, objStmCount: 0 },
    pageSummary: {
      analyzedPages: 0,
      uniformSize: true,
      mixedOrientation: false,
      variants: [],
      mismatchPages: [],
    },
    bleedAnalysis: { source: "none", minMm: null, maxMm: null },
    imageSummary: {
      count: 0,
      minWidthPx: null,
      minHeightPx: null,
      lowResCount: 0,
      lowResSuspects: [],
    },
  };
}

function toUint8Array(input) {
  if (input instanceof Uint8Array) return input;
  if (input instanceof ArrayBuffer) return new Uint8Array(input);
  if (ArrayBuffer.isView(input)) {
    return new Uint8Array(input.buffer, input.byteOffset, input.byteLength);
  }
  return null;
}

let cachedLatin1Decoder = null;
function decodeLatin1(bytes) {
  if (!bytes || bytes.byteLength === 0) return "";
  if (!cachedLatin1Decoder) {
    try {
      cachedLatin1Decoder = new TextDecoder("latin1");
    } catch {
      try {
        cachedLatin1Decoder = new TextDecoder("iso-8859-1");
      } catch {
        cachedLatin1Decoder = new TextDecoder("windows-1252");
      }
    }
  }
  return cachedLatin1Decoder.decode(bytes);
}

function round1(value) {
  return Math.round(value * 10) / 10;
}

function round2(value) {
  const rounded = Math.round(value * 100) / 100;
  return Object.is(rounded, -0) ? 0 : rounded;
}

function isPdfDelimiter(code) {
  return (
    code <= 32 ||
    code === 40 || // (
    code === 41 || // )
    code === 60 || // <
    code === 62 || // >
    code === 91 || // [
    code === 93 || // ]
    code === 123 || // {
    code === 125 || // }
    code === 47 || // /
    code === 37 // %
  );
}

/**
 * Find the matching `<< ... >>` dictionary immediately preceding `streamIndex`.
 */
function findPrecedingDict(rawText, streamIndex) {
  let cursor = streamIndex - 1;
  while (cursor >= 0 && rawText.charCodeAt(cursor) <= 32) cursor -= 1;
  if (cursor < 1 || rawText.charCodeAt(cursor) !== 62 || rawText.charCodeAt(cursor - 1) !== 62) {
    return null;
  }
  const dictEnd = cursor + 1;
  const minStart = Math.max(0, dictEnd - 16384);
  let depth = 0;
  let i = cursor;
  while (i >= minStart) {
    const ch = rawText.charCodeAt(i);
    if (ch === 62 && i > minStart && rawText.charCodeAt(i - 1) === 62) {
      depth += 1;
      i -= 2;
      continue;
    }
    if (ch === 60 && i > minStart && rawText.charCodeAt(i - 1) === 60) {
      depth -= 1;
      if (depth === 0) {
        return rawText.slice(i - 1, dictEnd);
      }
      i -= 2;
      continue;
    }
    i -= 1;
  }
  return null;
}

/**
 * Scan PDF byte stream for `stream ... endstream` blocks and return
 * metadata + ranges for stripping binary payloads from raw text.
 */
function scanStreams(bytes, rawText) {
  const streams = [];
  const binaryRanges = [];
  const totalLen = rawText.length;
  let searchPos = 0;

  while (searchPos < totalLen) {
    const idx = rawText.indexOf("stream", searchPos);
    if (idx === -1) break;

    const prevOk = idx === 0 || isPdfDelimiter(rawText.charCodeAt(idx - 1));
    const afterKeyword = idx + 6;
    if (!prevOk || afterKeyword >= totalLen) {
      searchPos = afterKeyword;
      continue;
    }

    let dataStart = -1;
    const c1 = rawText.charCodeAt(afterKeyword);
    const c2 = afterKeyword + 1 < totalLen ? rawText.charCodeAt(afterKeyword + 1) : -1;
    if (c1 === 13 && c2 === 10) {
      dataStart = afterKeyword + 2;
    } else if (c1 === 10 || c1 === 13) {
      dataStart = afterKeyword + 1;
    } else {
      searchPos = afterKeyword;
      continue;
    }

    const dictText = findPrecedingDict(rawText, idx);
    if (!dictText) {
      searchPos = afterKeyword;
      continue;
    }

    // Determine exact stream end using direct /Length when valid, else next endstream.
    let dataEnd = -1;
    let nextPos = dataStart;
    const directLenMatch = dictText.match(/\/Length\s+(\d+)(?!\s+\d+\s+R)\b/);
    if (directLenMatch) {
      const declaredLen = Number.parseInt(directLenMatch[1], 10);
      if (Number.isFinite(declaredLen) && declaredLen >= 0 && dataStart + declaredLen <= totalLen) {
        const probeStart = dataStart + declaredLen;
        const probeSlice = rawText.slice(probeStart, Math.min(totalLen, probeStart + 32));
        const endMatch = probeSlice.match(/^\s*endstream\b/);
        if (endMatch) {
          dataEnd = probeStart;
          nextPos = probeStart + endMatch[0].length;
        }
      }
    }

    if (dataEnd === -1) {
      const endIdx = rawText.indexOf("endstream", dataStart);
      if (endIdx === -1) break;
      dataEnd = endIdx;
      if (dataEnd > dataStart && rawText.charCodeAt(dataEnd - 1) === 10) dataEnd -= 1;
      if (dataEnd > dataStart && rawText.charCodeAt(dataEnd - 1) === 13) dataEnd -= 1;
      nextPos = endIdx + 9;
    }

    binaryRanges.push([dataStart, dataEnd]);

    const hasFlate = /\/FlateDecode(?![A-Za-z0-9_])/.test(dictText);
    const hasOtherFilter = UNSUPPORTED_FILTERS.some((name) =>
      new RegExp(`/${name}(?![A-Za-z0-9_])`).test(dictText)
    );
    const isObjStm = /\/Type\s*\/ObjStm(?![A-Za-z0-9_])/.test(dictText);
    const isXRef = /\/Type\s*\/XRef(?![A-Za-z0-9_])/.test(dictText);
    const isImage = /\/Subtype\s*\/Image(?![A-Za-z0-9_])/.test(dictText);
    const isFontStream =
      /\/Length1\b/.test(dictText) ||
      /\/Subtype\s*\/(?:Type1C|CIDFontType0C|OpenType)(?![A-Za-z0-9_])/.test(dictText);

    if (hasFlate && !hasOtherFilter && !isImage && !isFontStream && dataEnd > dataStart) {
      const firstMatch = dictText.match(/\/First\s+(\d+)/);
      const nMatch = dictText.match(/\/N\s+(\d+)/);
      const hasPredictor = /\/Predictor\s+([2-9]|\d{2,})/.test(dictText);
      streams.push({
        dataStart,
        dataEnd,
        length: dataEnd - dataStart,
        isObjStm,
        isXRef,
        hasPredictor,
        first: firstMatch ? Number.parseInt(firstMatch[1], 10) : null,
        count: nMatch ? Number.parseInt(nMatch[1], 10) : null,
        priority: isObjStm ? 0 : isXRef ? 1 : 2,
      });
    }

    searchPos = nextPos;
  }

  return { streams, binaryRanges };
}

/**
 * Strip binary stream payloads from raw PDF text so downstream regex & dict
 * scanners never confuse compressed bytes with PDF syntax tokens.
 */
function stripBinaryRanges(rawText, binaryRanges) {
  if (!binaryRanges.length) return rawText;
  const parts = [];
  let cursor = 0;
  for (const [start, end] of binaryRanges) {
    if (start > cursor) parts.push(rawText.slice(cursor, start));
    parts.push("\n");
    cursor = Math.max(cursor, end);
  }
  if (cursor < rawText.length) parts.push(rawText.slice(cursor));
  return parts.join("");
}

/**
 * Bounded native Web Streams inflate using DecompressionStream.
 */
async function tryDecompress(slice, format, maxOutputBytes) {
  if (typeof DecompressionStream !== "function") return null;
  let ds;
  try {
    ds = new DecompressionStream(format);
  } catch {
    return null;
  }
  const writer = ds.writable.getWriter();
  const reader = ds.readable.getReader();

  const writePromise = (async () => {
    try {
      await writer.write(slice);
      await writer.close();
    } catch {
      // Ignore write errors when reader aborts or stream is malformed.
    }
  })();

  const chunks = [];
  let total = 0;
  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      if (!value || value.byteLength === 0) continue;
      if (total + value.byteLength > maxOutputBytes) {
        const remain = maxOutputBytes - total;
        if (remain > 0) chunks.push(value.subarray(0, remain));
        total = maxOutputBytes;
        await reader.cancel().catch(() => {});
        await writer.abort().catch(() => {});
        break;
      }
      chunks.push(value);
      total += value.byteLength;
    }
  } catch {
    await writer.abort().catch(() => {});
    await writePromise;
    return null;
  }

  await writePromise;
  if (total === 0) return null;
  const output = new Uint8Array(total);
  let offset = 0;
  for (const chunk of chunks) {
    output.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return output;
}

async function inflateFlateStream(slice, maxOutputBytes) {
  const inflated = await tryDecompress(slice, "deflate", maxOutputBytes);
  if (inflated) return inflated;

  const rawInflated = await tryDecompress(slice, "deflate-raw", maxOutputBytes);
  if (rawInflated) return rawInflated;

  // Fallback for streams carrying a 2-byte zlib header with a missing/bad trailer.
  if (slice.byteLength > 2 && (slice[0] & 0x0f) === 8) {
    return tryDecompress(slice.subarray(2), "deflate-raw", maxOutputBytes);
  }
  return null;
}

/**
 * Split a decompressed `/Type /ObjStm` buffer into individual `{ objNum, text }` entries.
 */
function parseObjStmEntries(decodedText, first, count) {
  if (!Number.isFinite(first) || first <= 0 || first >= decodedText.length) {
    return null;
  }
  const headerSlice = decodedText.slice(0, first);
  const tokens = headerSlice.match(/\d+/g);
  if (!tokens || tokens.length < 2) return null;

  const pairCount = Number.isFinite(count) && count > 0
    ? Math.min(count, Math.floor(tokens.length / 2))
    : Math.floor(tokens.length / 2);
  if (pairCount <= 0) return null;

  const indexPairs = [];
  for (let i = 0; i < pairCount; i += 1) {
    const objNum = Number.parseInt(tokens[i * 2], 10);
    const relOffset = Number.parseInt(tokens[i * 2 + 1], 10);
    if (!Number.isFinite(objNum) || !Number.isFinite(relOffset) || relOffset < 0) continue;
    indexPairs.push({ objNum, absOffset: first + relOffset });
  }
  if (!indexPairs.length) return null;

  const entries = [];
  for (let i = 0; i < indexPairs.length; i += 1) {
    const start = indexPairs[i].absOffset;
    const end = i + 1 < indexPairs.length ? indexPairs[i + 1].absOffset : decodedText.length;
    if (start >= 0 && start < decodedText.length && end > start) {
      entries.push({
        objNum: indexPairs[i].objNum,
        text: decodedText.slice(start, Math.min(end, decodedText.length)),
      });
    }
  }
  return entries.length ? entries : null;
}

/**
 * Extract balanced `<< ... >>` top-level dictionaries from a PDF syntax snippet.
 */
function extractTopDictionaries(text) {
  const dicts = [];
  const len = text.length;
  let i = 0;
  let depth = 0;
  let start = -1;

  while (i < len) {
    const ch = text.charCodeAt(i);

    // PDF comments (% ... EOL)
    if (ch === 37) {
      while (i < len && text.charCodeAt(i) !== 10 && text.charCodeAt(i) !== 13) i += 1;
      continue;
    }

    // Literal string (...) with balanced parens and escapes
    if (ch === 40) {
      let parenDepth = 1;
      i += 1;
      while (i < len && parenDepth > 0) {
        const c = text.charCodeAt(i);
        if (c === 92) {
          i += 2;
          continue;
        }
        if (c === 40) parenDepth += 1;
        else if (c === 41) parenDepth -= 1;
        i += 1;
      }
      continue;
    }

    // Dictionary open `<<` vs hex string `<...>`
    if (ch === 60) {
      if (i + 1 < len && text.charCodeAt(i + 1) === 60) {
        if (depth === 0) start = i;
        depth += 1;
        i += 2;
        continue;
      }
      // Hex string `<...>`
      i += 1;
      while (i < len && text.charCodeAt(i) !== 62) i += 1;
      i += 1;
      continue;
    }

    // Dictionary close `>>`
    if (ch === 62 && i + 1 < len && text.charCodeAt(i + 1) === 62) {
      if (depth > 0) {
        depth -= 1;
        if (depth === 0 && start !== -1) {
          dicts.push(text.slice(start, i + 2));
          start = -1;
        }
      }
      i += 2;
      continue;
    }

    i += 1;
  }

  return dicts;
}

function parseBoxFromText(text, boxName) {
  const pattern = new RegExp(
    `/${boxName}\\s*\\[\\s*(-?\\d+(?:\\.\\d+)?|-?\\.\\d+)\\s+(-?\\d+(?:\\.\\d+)?|-?\\.\\d+)\\s+(-?\\d+(?:\\.\\d+)?|-?\\.\\d+)\\s+(-?\\d+(?:\\.\\d+)?|-?\\.\\d+)\\s*\\]`
  );
  const match = text.match(pattern);
  if (!match) return null;
  const nums = match.slice(1).map(Number);
  if (nums.some((n) => !Number.isFinite(n))) return null;
  return nums;
}

function normalizeRect(box) {
  if (!Array.isArray(box) || box.length !== 4) return null;
  const [x1, y1, x2, y2] = box;
  if (![x1, y1, x2, y2].every((v) => typeof v === "number" && Number.isFinite(v))) return null;
  return [Math.min(x1, x2), Math.min(y1, y2), Math.max(x1, x2), Math.max(y1, y2)];
}

function parseRotate(text) {
  const match = text.match(/\/Rotate\s+(-?\d+)\b/);
  if (!match) return null;
  const raw = Number.parseInt(match[1], 10);
  if (!Number.isFinite(raw)) return null;
  const norm = ((raw % 360) + 360) % 360;
  return norm === 0 || norm === 90 || norm === 180 || norm === 270 ? norm : 0;
}

function parseKidsRefs(text) {
  const match = text.match(/\/Kids\s*\[([\s\S]*?)\]/);
  if (!match) return [];
  const refs = [];
  const refRe = /(\d+)\s+\d+\s+R/g;
  let m;
  while ((m = refRe.exec(match[1])) !== null) {
    const num = Number.parseInt(m[1], 10);
    if (Number.isFinite(num)) refs.push(num);
  }
  return refs;
}

function parseParentRef(text) {
  const match = text.match(/\/Parent\s+(\d+)\s+\d+\s+R/);
  if (!match) return null;
  const num = Number.parseInt(match[1], 10);
  return Number.isFinite(num) ? num : null;
}

function parsePageDict(dictText, objNum) {
  return {
    objNum,
    parent: parseParentRef(dictText),
    media: parseBoxFromText(dictText, "MediaBox"),
    crop: parseBoxFromText(dictText, "CropBox"),
    trim: parseBoxFromText(dictText, "TrimBox"),
    bleed: parseBoxFromText(dictText, "BleedBox"),
    rotate: parseRotate(dictText),
  };
}

function computePageMetrics(page) {
  const effectiveBox = page.trim || page.crop || page.media;
  const rect = normalizeRect(effectiveBox);
  if (!rect) return null;
  const rawW = (rect[2] - rect[0]) * PT_TO_MM;
  const rawH = (rect[3] - rect[1]) * PT_TO_MM;
  if (rawW <= 0 || rawH <= 0) return null;
  const rot = page.rotate || 0;
  const swapped = rot === 90 || rot === 270;
  const widthMm = round1(swapped ? rawH : rawW);
  const heightMm = round1(swapped ? rawW : rawH);
  let orientation = "portrait";
  if (Math.abs(widthMm - heightMm) <= SIZE_TOLERANCE_MM) {
    orientation = "square";
  } else if (widthMm > heightMm) {
    orientation = "landscape";
  }
  return { widthMm, heightMm, orientation };
}

function computeBleedAnalysis(pages, fallbackBoxes) {
  const candidates = [];
  for (const page of pages) {
    candidates.push({ media: page.media, trim: page.trim, bleed: page.bleed });
  }
  candidates.push(fallbackBoxes);

  // Prefer BleedBox vs TrimBox
  for (const item of candidates) {
    const trimRect = normalizeRect(item.trim);
    const bleedRect = normalizeRect(item.bleed);
    if (trimRect && bleedRect) {
      const left = round2((trimRect[0] - bleedRect[0]) * PT_TO_MM);
      const right = round2((bleedRect[2] - trimRect[2]) * PT_TO_MM);
      const bottom = round2((trimRect[1] - bleedRect[1]) * PT_TO_MM);
      const top = round2((bleedRect[3] - trimRect[3]) * PT_TO_MM);
      const sides = [left, right, bottom, top];
      return {
        source: "bleed_vs_trim",
        minMm: round2(Math.min(...sides)),
        maxMm: round2(Math.max(...sides)),
        sidesMm: { left, right, bottom, top },
      };
    }
  }

  // Fallback to MediaBox vs TrimBox when MediaBox slightly exceeds TrimBox (1..15mm)
  for (const item of candidates) {
    const trimRect = normalizeRect(item.trim);
    const mediaRect = normalizeRect(item.media);
    if (trimRect && mediaRect) {
      const left = round2((trimRect[0] - mediaRect[0]) * PT_TO_MM);
      const right = round2((mediaRect[2] - trimRect[2]) * PT_TO_MM);
      const bottom = round2((trimRect[1] - mediaRect[1]) * PT_TO_MM);
      const top = round2((mediaRect[3] - trimRect[3]) * PT_TO_MM);
      const sides = [left, right, bottom, top];
      const minSide = Math.min(...sides);
      const maxSide = Math.max(...sides);
      if (minSide >= -0.05 && maxSide >= 1 && maxSide <= 15) {
        return {
          source: "media_vs_trim",
          minMm: Math.max(0, round2(minSide)),
          maxMm: round2(maxSide),
          sidesMm: { left, right, bottom, top },
        };
      }
    }
  }

  return { source: "none", minMm: null, maxMm: null };
}

function summarizePages(orderedPages) {
  const analyzed = [];
  orderedPages.forEach((page, index) => {
    const metrics = computePageMetrics(page);
    if (metrics) {
      analyzed.push({ pageNumber: index + 1, ...metrics });
    }
  });

  if (!analyzed.length) {
    return {
      analyzedPages: 0,
      uniformSize: true,
      mixedOrientation: false,
      variants: [],
      mismatchPages: [],
    };
  }

  const first = analyzed[0];
  let uniformSize = true;
  let hasLandscape = false;
  let hasPortrait = false;
  const mismatchPages = [];
  const variants = [];

  for (const item of analyzed) {
    if (item.orientation === "landscape") hasLandscape = true;
    if (item.orientation === "portrait") hasPortrait = true;

    const exactMatch =
      Math.abs(item.widthMm - first.widthMm) <= SIZE_TOLERANCE_MM &&
      Math.abs(item.heightMm - first.heightMm) <= SIZE_TOLERANCE_MM;
    const rotatedMatch =
      Math.abs(item.widthMm - first.heightMm) <= SIZE_TOLERANCE_MM &&
      Math.abs(item.heightMm - first.widthMm) <= SIZE_TOLERANCE_MM;

    if (!exactMatch && !rotatedMatch) {
      uniformSize = false;
    }
    if (!exactMatch && mismatchPages.length < MAX_MISMATCH_PAGES) {
      mismatchPages.push(item.pageNumber);
    }

    let bucket = variants.find(
      (v) =>
        Math.abs(v.widthMm - item.widthMm) <= SIZE_TOLERANCE_MM &&
        Math.abs(v.heightMm - item.heightMm) <= SIZE_TOLERANCE_MM
    );
    if (!bucket) {
      bucket = {
        widthMm: item.widthMm,
        heightMm: item.heightMm,
        orientation: item.orientation,
        count: 0,
        samplePages: [],
      };
      variants.push(bucket);
    }
    bucket.count += 1;
    if (bucket.samplePages.length < MAX_SAMPLE_PAGES) {
      bucket.samplePages.push(item.pageNumber);
    }
  }

  variants.sort((a, b) => b.count - a.count);

  return {
    analyzedPages: analyzed.length,
    uniformSize,
    mixedOrientation: hasLandscape && hasPortrait,
    variants: variants.slice(0, MAX_VARIANTS),
    mismatchPages,
  };
}

function summarizeImages(imageDicts, referencePageMetrics) {
  let count = 0;
  let minWidthPx = null;
  let minHeightPx = null;
  let lowResCount = 0;
  const suspectCandidates = [];

  const pageInW = referencePageMetrics ? referencePageMetrics.widthMm / 25.4 : null;
  const pageInH = referencePageMetrics ? referencePageMetrics.heightMm / 25.4 : null;
  const maxPageIn = pageInW && pageInH ? Math.max(pageInW, pageInH) : null;
  const minPageIn = pageInW && pageInH ? Math.min(pageInW, pageInH) : null;

  for (const dict of imageDicts) {
    count += 1;
    const wMatch = dict.match(/\/Width\s+(\d+)\b/);
    const hMatch = dict.match(/\/Height\s+(\d+)\b/);
    if (!wMatch || !hMatch) continue;
    const widthPx = Number.parseInt(wMatch[1], 10);
    const heightPx = Number.parseInt(hMatch[1], 10);
    if (!Number.isFinite(widthPx) || !Number.isFinite(heightPx) || widthPx <= 0 || heightPx <= 0) {
      continue;
    }

    if (minWidthPx === null || widthPx < minWidthPx) minWidthPx = widthPx;
    if (minHeightPx === null || heightPx < minHeightPx) minHeightPx = heightPx;

    // Ignore 1px/16px/32px masks and tiny decorative dots to avoid false alarms.
    const isImageMask = /\/ImageMask\s+true\b/.test(dict);
    if (isImageMask || widthPx <= 32 || heightPx <= 32) continue;

    let estimatedDpi = null;
    let isLowRes = false;
    if (maxPageIn && minPageIn) {
      const maxPx = Math.max(widthPx, heightPx);
      const minPx = Math.min(widthPx, heightPx);
      estimatedDpi = Math.round(Math.min(maxPx / maxPageIn, minPx / minPageIn));
      if (estimatedDpi < 180) {
        isLowRes = true;
      } else if ((widthPx < 300 || heightPx < 300) && estimatedDpi < 250) {
        isLowRes = true;
      }
    } else if (widthPx < 300 || heightPx < 300) {
      isLowRes = true;
    }

    if (isLowRes) {
      lowResCount += 1;
      suspectCandidates.push({ widthPx, heightPx, estimatedDpi });
    }
  }

  suspectCandidates.sort((a, b) => a.widthPx * a.heightPx - b.widthPx * b.heightPx);

  return {
    count,
    minWidthPx,
    minHeightPx,
    lowResCount,
    lowResSuspects: suspectCandidates.slice(0, MAX_LOW_RES_SAMPLES),
  };
}

function enforcePayloadBudget(result) {
  try {
    const encoded = JSON.stringify(result);
    if (encoded.length <= MAX_JSON_BYTES) return result;
    if (result.pageSummary?.variants?.length > 2) {
      result.pageSummary.variants = result.pageSummary.variants.slice(0, 2);
    }
    if (result.imageSummary?.lowResSuspects?.length > 2) {
      result.imageSummary.lowResSuspects = result.imageSummary.lowResSuspects.slice(0, 2);
    }
    if (result.bleedAnalysis?.sidesMm) {
      delete result.bleedAnalysis.sidesMm;
    }
  } catch {
    // Keep result unchanged if serialization fails unexpectedly.
  }
  return result;
}

/**
 * Inspect raw PDF bytes locally (browser & Node.js 18+ compatible).
 * Never throws on malformed or truncated inputs.
 */
export async function inspectPdfBytes(input) {
  try {
    const bytes = toUint8Array(input);
    if (!bytes || bytes.byteLength === 0) {
      return createFallbackInspection();
    }

    const headerSlice = decodeLatin1(bytes.subarray(0, Math.min(1024, bytes.byteLength)));
    const tailSlice = decodeLatin1(bytes.subarray(Math.max(0, bytes.byteLength - 2048)));
    const headerIdx = headerSlice.indexOf("%PDF-");
    const isPdf = headerIdx >= 0 && headerIdx <= 16;
    const pdfVersionMatch = headerSlice.match(/%PDF-(\d+\.\d+)/);
    const pdfVersion = pdfVersionMatch ? pdfVersionMatch[1].slice(0, 12) : null;
    const hasEof = tailSlice.includes("%%EOF");

    if (!isPdf) {
      return {
        ...createFallbackInspection(),
        readable: true,
        isPdf: false,
        pdfVersion: null,
        hasEof,
      };
    }

    const rawText = decodeLatin1(bytes);
    const { streams, binaryRanges } = scanStreams(bytes, rawText);
    const strippedRawText = stripBinaryRanges(rawText, binaryRanges);

    // Prioritize ObjStm and XRef streams before generic content streams.
    const sortedStreams = [...streams].sort((a, b) => a.priority - b.priority);
    const detectedCount = sortedStreams.length;
    const objStmCount = sortedStreams.filter((s) => s.isObjStm).length;
    let decodedCount = 0;
    let totalDecodedBytes = 0;
    const decompressedTextChunks = [];
    const objStmEntries = [];

    for (const stream of sortedStreams) {
      if (decodedCount >= MAX_DECOMPRESSED_STREAMS) break;
      if (totalDecodedBytes >= MAX_TOTAL_OUTPUT_BYTES) break;
      if (stream.hasPredictor && stream.isXRef) continue;

      const inputLimit = stream.isObjStm || stream.isXRef
        ? MAX_OBJSTM_INPUT_BYTES
        : MAX_GENERIC_STREAM_INPUT_BYTES;
      if (stream.length > inputLimit) continue;

      const remainingBudget = Math.min(
        MAX_STREAM_OUTPUT_BYTES,
        MAX_TOTAL_OUTPUT_BYTES - totalDecodedBytes
      );
      if (remainingBudget <= 0) break;

      const slice = bytes.subarray(stream.dataStart, stream.dataEnd);
      const inflated = await inflateFlateStream(slice, remainingBudget);
      if (!inflated) continue;

      decodedCount += 1;
      totalDecodedBytes += inflated.byteLength;
      const decodedText = decodeLatin1(inflated);

      if (stream.isObjStm) {
        const parsed = parseObjStmEntries(decodedText, stream.first, stream.count);
        if (parsed) {
          objStmEntries.push(...parsed);
        } else {
          decompressedTextChunks.push(decodedText);
        }
      } else {
        decompressedTextChunks.push(decodedText);
      }
    }

    // Collect indirect objects from raw text and ObjStm entries (latest revision wins per objNum).
    const objectMap = new Map();
    const anonymousBlocks = [];
    const objRegex = /(\d+)\s+(\d+)\s+obj\b([\s\S]*?)\bendobj\b/g;
    let match;
    let lastEnd = 0;
    let matchedAnyObj = false;

    while ((match = objRegex.exec(strippedRawText)) !== null) {
      matchedAnyObj = true;
      const objNum = Number.parseInt(match[1], 10);
      const body = match[3];
      if (Number.isFinite(objNum)) {
        objectMap.set(objNum, body);
      }
      lastEnd = objRegex.lastIndex;
    }

    if (!matchedAnyObj) {
      anonymousBlocks.push(strippedRawText);
    } else if (lastEnd < strippedRawText.length) {
      // Capture trailer / trailing dictionaries outside obj...endobj
      anonymousBlocks.push(strippedRawText.slice(lastEnd));
    }

    for (const entry of objStmEntries) {
      objectMap.set(entry.objNum, entry.text);
    }
    for (const chunk of decompressedTextChunks) {
      anonymousBlocks.push(chunk);
    }

    // Classify Pages nodes, Page objects, and Image objects across all objects.
    const pagesNodes = new Map();
    const pagesList = [];
    const pageObjectsByNum = new Map();
    const loosePages = [];
    const imageDicts = [];
    let maxPagesCount = 0;

    const inspectBlock = (text, objNum) => {
      const dicts = extractTopDictionaries(text);
      for (const dict of dicts) {
        if (/\/Type\s*\/Pages(?![A-Za-z0-9_])/.test(dict)) {
          const countMatch = dict.match(/\/Count\s+(\d+)\b/);
          const count = countMatch ? Number.parseInt(countMatch[1], 10) : 0;
          if (Number.isFinite(count) && count > maxPagesCount && count <= 100000) {
            maxPagesCount = count;
          }
          const node = {
            objNum,
            parent: parseParentRef(dict),
            count,
            kids: parseKidsRefs(dict),
            media: parseBoxFromText(dict, "MediaBox"),
            crop: parseBoxFromText(dict, "CropBox"),
            rotate: parseRotate(dict),
          };
          if (objNum !== null) pagesNodes.set(objNum, node);
          pagesList.push(node);
        } else if (/\/Type\s*\/Page(?![A-Za-z0-9_])/.test(dict)) {
          const page = parsePageDict(dict, objNum);
          if (objNum !== null) {
            pageObjectsByNum.set(objNum, page);
          } else {
            loosePages.push(page);
          }
        } else if (/\/Subtype\s*\/Image(?![A-Za-z0-9_])/.test(dict)) {
          imageDicts.push(dict);
        }
      }
    };

    for (const [objNum, body] of objectMap.entries()) {
      inspectBlock(body, objNum);
    }
    for (const block of anonymousBlocks) {
      inspectBlock(block, null);
    }

    // Inherit MediaBox / CropBox / Rotate from ancestor /Pages nodes when missing on a /Page.
    const defaultPagesNode = pagesList.find((n) => n.media || n.crop || n.rotate !== null) || null;
    const resolveInherited = (page) => {
      let currentParent = page.parent;
      let hops = 0;
      const visited = new Set();
      while (currentParent !== null && hops < 16 && !visited.has(currentParent)) {
        visited.add(currentParent);
        const parentNode = pagesNodes.get(currentParent);
        if (!parentNode) break;
        if (!page.media && parentNode.media) page.media = parentNode.media;
        if (!page.crop && parentNode.crop) page.crop = parentNode.crop;
        if (page.rotate === null && parentNode.rotate !== null) page.rotate = parentNode.rotate;
        currentParent = parentNode.parent;
        hops += 1;
      }
      if (defaultPagesNode) {
        if (!page.media && defaultPagesNode.media) page.media = defaultPagesNode.media;
        if (!page.crop && defaultPagesNode.crop) page.crop = defaultPagesNode.crop;
        if (page.rotate === null && defaultPagesNode.rotate !== null) page.rotate = defaultPagesNode.rotate;
      }
      if (page.rotate === null) page.rotate = 0;
      return page;
    };

    for (const page of pageObjectsByNum.values()) resolveInherited(page);
    for (const page of loosePages) resolveInherited(page);

    // Order pages via the /Pages tree /Kids sequence when available.
    const orderedPages = [];
    const placedObjNums = new Set();
    const childPagesSet = new Set();
    for (const node of pagesNodes.values()) {
      for (const kid of node.kids) {
        if (pagesNodes.has(kid)) childPagesSet.add(kid);
      }
    }
    const rootNodes = [...pagesNodes.values()]
      .filter((node) => !childPagesSet.has(node.objNum))
      .sort((a, b) => b.count - a.count);

    const visitedNodes = new Set();
    const traverseKids = (node) => {
      if (!node || visitedNodes.has(node.objNum)) return;
      visitedNodes.add(node.objNum);
      for (const kid of node.kids) {
        if (pagesNodes.has(kid)) {
          traverseKids(pagesNodes.get(kid));
        } else if (pageObjectsByNum.has(kid) && !placedObjNums.has(kid)) {
          orderedPages.push(pageObjectsByNum.get(kid));
          placedObjNums.add(kid);
        }
      }
    };
    for (const root of rootNodes) traverseKids(root);
    for (const [objNum, page] of pageObjectsByNum.entries()) {
      if (!placedObjNums.has(objNum)) {
        orderedPages.push(page);
        placedObjNums.add(objNum);
      }
    }
    orderedPages.push(...loosePages);

    // Build combined searchable corpus from deduplicated objects + anonymous blocks.
    const corpusParts = [];
    for (const body of objectMap.values()) corpusParts.push(body);
    for (const block of anonymousBlocks) corpusParts.push(block);
    const fullCorpus = corpusParts.join("\n");

    // Cross-check page count from resolved /Page objects vs /Pages /Count.
    const pageCount =
      maxPagesCount > orderedPages.length
        ? maxPagesCount
        : orderedPages.length > 0
          ? orderedPages.length
          : maxPagesCount;

    // Backwards-compatible top-level boxes (first page box with document-wide fallback).
    const pickBox = (key, pdfName) => {
      for (const page of orderedPages) {
        if (page[key]) return page[key];
      }
      return parseBoxFromText(fullCorpus, pdfName);
    };
    const boxes = {
      media: pickBox("media", "MediaBox"),
      crop: pickBox("crop", "CropBox"),
      trim: pickBox("trim", "TrimBox"),
      bleed: pickBox("bleed", "BleedBox"),
    };

    const pageSummary = summarizePages(orderedPages);
    const bleedAnalysis = computeBleedAnalysis(orderedPages, boxes);

    const refPageMetrics = orderedPages.length
      ? computePageMetrics(orderedPages[0])
      : boxes.trim || boxes.media
        ? computePageMetrics({ trim: boxes.trim, crop: boxes.crop, media: boxes.media, rotate: 0 })
        : null;
    const imageSummary = summarizeImages(imageDicts, refPageMetrics);

    const colorSpaces = ["DeviceRGB", "DeviceCMYK", "Separation", "DeviceN"].filter((name) =>
      new RegExp(`/${name}(?![A-Za-z0-9_])`).test(fullCorpus)
    );
    const encrypted =
      /\/Encrypt(?![A-Za-z0-9_])/.test(fullCorpus) ||
      /\/Encrypt(?![A-Za-z0-9_])/.test(strippedRawText);
    const fontEmbedding = /\/FontFile(?:2|3)?(?![A-Za-z0-9_])/.test(fullCorpus) ? "embedded" : "unknown";
    const regexImageCount = (fullCorpus.match(/\/Subtype\s*\/Image(?![A-Za-z0-9_])/g) || []).length;
    const imageCount = Math.max(imageSummary.count, regexImageCount);
    if (imageSummary.count < imageCount) imageSummary.count = imageCount;

    const hasTransparency =
      /\/SMask(?![A-Za-z0-9_])(?!\s*\/None(?![A-Za-z0-9_]))/.test(fullCorpus) ||
      /\/(?:ca|CA)\s+0?\.\d+/.test(fullCorpus);
    const hasOverprint = /\/(?:OP|op)(?![A-Za-z0-9_])(?!\s+false\b)/.test(fullCorpus);

    const result = {
      readable: true,
      isPdf: true,
      pdfVersion,
      pageCount,
      encrypted,
      hasEof,
      boxes,
      colorSpaces,
      fontEmbedding,
      imageCount,
      hasTransparency,
      hasOverprint,
      compressedStreams: {
        detected: detectedCount,
        decoded: decodedCount,
        objStmCount,
      },
      pageSummary,
      bleedAnalysis,
      imageSummary,
    };

    return enforcePayloadBudget(result);
  } catch {
    return createFallbackInspection();
  }
}
