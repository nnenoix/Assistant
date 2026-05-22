/**
 * Phase 14C — Persistent Aggregator.
 *
 * Single Apps Script project, deployed once as API executable. Called from
 * Python via clasp's `run` command (see src/tools/apps_script.py:run_function).
 *
 * Why persistent (not disposable per-call):
 *   1. Disposable apps_script_oneshot creates a NEW project each call. New
 *      projects need a manual "Deploy as API executable" click — can't be
 *      automated. Persistent project = deploy once, call forever.
 *   2. One quota token per call regardless of how many spreadsheets opened
 *      inside (vs N tokens for direct Sheets API read).
 *   3. CacheService allows resumption: if the 6-min execution limit is
 *      reached, save progress + return a token; client re-invokes to continue.
 *
 * Functions:
 *   - cross_aggregate(ids, sheet, cell, op, resumeToken?) — main entry
 *   - cross_aggregate_status(token) — peek at progress of an incomplete run
 *   - ping() — sanity check for verify_phase14_setup.py
 */

// Safety margin under the 6-min hard timeout. Apps Script v8 typically
// gives ~6 min for consumer accounts; we save state at 4.5 min and let
// the client re-invoke.
var MAX_RUN_MS = 4.5 * 60 * 1000;

// CacheService TTL for resumable state. Long enough for client to poll
// + re-invoke; not so long that orphans accumulate.
var CACHE_TTL_S = 1800; // 30 min

var SUPPORTED_OPS = ['sum', 'avg', 'min', 'max', 'count', 'list'];


/**
 * Read `sheet!cell` from each id in `ids`, apply `op`.
 *
 * @param {string[]} ids - spreadsheet IDs to read from
 * @param {string} sheet - tab name, e.g. "Год факт"
 * @param {string} cell - A1 ref, e.g. "B45"
 * @param {string} op - one of SUPPORTED_OPS
 * @param {string=} resumeToken - if set, continues a previous incomplete run
 * @return {object} complete result OR {status: "incomplete", token, ...}
 */
function cross_aggregate(ids, sheet, cell, op, resumeToken) {
  var t0 = Date.now();
  var cache = CacheService.getScriptCache();

  var processed = [];
  var errors = [];
  var startIndex = 0;

  // Resume state if a token was passed
  if (resumeToken) {
    var saved = cache.get(resumeToken);
    if (saved) {
      var state = JSON.parse(saved);
      processed = state.processed || [];
      errors = state.errors || [];
      startIndex = state.startIndex || 0;
      ids = state.ids;
      sheet = state.sheet;
      cell = state.cell;
      op = state.op;
    } else {
      return {
        status: 'error',
        reason: 'resumeToken expired or not found',
        token: resumeToken
      };
    }
  }

  // Validate op
  if (SUPPORTED_OPS.indexOf(op) === -1) {
    return {
      status: 'error',
      reason: 'unknown op: ' + op + ' (expected one of ' + SUPPORTED_OPS.join('|') + ')'
    };
  }

  // Iterate spreadsheets
  for (var i = startIndex; i < ids.length; i++) {
    // Time budget check — save and bail before 6-min wall
    if (Date.now() - t0 > MAX_RUN_MS) {
      var token = resumeToken || Utilities.getUuid();
      cache.put(token, JSON.stringify({
        ids: ids, sheet: sheet, cell: cell, op: op,
        processed: processed, errors: errors, startIndex: i
      }), CACHE_TTL_S);
      var incompleteResult = {
        status: 'incomplete',
        token: token,
        processed_count: processed.length,
        errors_count: errors.length,
        remaining_count: ids.length - i,
        _meta: { op: op, sheet: sheet, cell: cell, elapsed_ms: Date.now() - t0 }
      };
      console.log(JSON.stringify(incompleteResult));
      return incompleteResult;
    }

    try {
      var ss = SpreadsheetApp.openById(ids[i]);
      var sh = ss.getSheetByName(sheet);
      if (!sh) {
        errors.push({ id: ids[i], kind: 'sheet_not_found',
                      msg: "no sheet named '" + sheet + "'" });
        continue;
      }
      var value = sh.getRange(cell).getValue();
      processed.push({ id: ids[i], value: value });
    } catch (e) {
      errors.push({ id: ids[i], kind: 'open_or_read_error',
                    msg: String(e).slice(0, 200) });
    }
  }

  // Apply op
  var opResult = _applyOp(op, processed);

  // Cleanup resume token
  if (resumeToken) cache.remove(resumeToken);

  var result = {
    status: 'complete',
    value: opResult,
    per_file_count: processed.length,
    errors_count: errors.length,
    errors: errors.slice(0, 10),
    _meta: {
      op: op,
      sheet: sheet,
      cell: cell,
      duration_ms: Date.now() - t0,
      total_input_count: ids.length
    }
  };
  // clasp run doesn't surface `return` to stdout — emit via console.log
  // so the Python orchestrator can parse it.
  console.log(JSON.stringify(result));
  return result;
}


/**
 * Inspect an incomplete run's saved state without resuming.
 * @param {string} token - resume token returned by a prior cross_aggregate
 * @return {object} {status, processed_count, remaining_count} or {status:"not_found"}
 */
function cross_aggregate_status(token) {
  var cache = CacheService.getScriptCache();
  var saved = cache.get(token);
  var result;
  if (!saved) {
    result = { status: 'not_found', token: token };
  } else {
    var state = JSON.parse(saved);
    result = {
      status: 'incomplete',
      token: token,
      processed_count: (state.processed || []).length,
      errors_count: (state.errors || []).length,
      remaining_count: (state.ids || []).length - (state.startIndex || 0),
      _meta: { op: state.op, sheet: state.sheet, cell: state.cell }
    };
  }
  console.log(JSON.stringify(result));
  return result;
}


/**
 * Trivial health probe — proves the Apps Script project is deployed and
 * reachable via the Execution API. Used by scripts/verify_phase14_setup.py.
 * @return {object} {ok: true, version: "phase14", runtime: "V8"}
 */
function ping() {
  var result = { ok: true, version: 'phase14', runtime: 'V8', server_time: new Date().toISOString() };
  console.log(JSON.stringify(result));
  return result;
}


/**
 * Populate a spreadsheet with `target_rows` × `target_cols` of synthetic
 * numeric data on `sheet_name`. Resizes the grid if necessary. Resumes via
 * resumeFromRow if a previous call returned 'incomplete'.
 *
 * Why server-side: setValues() inside Apps Script is ~5x faster than the
 * Sheets API equivalent from Python. Critical for building Tier-B-HEAVY
 * fixtures (50 books × ~4M cells = 200M cells population).
 *
 * @param {string} spreadsheet_id
 * @param {string} sheet_name (created if missing)
 * @param {number} target_rows (e.g. 5000)
 * @param {number} target_cols (e.g. 800)
 * @param {number=} resumeFromRow - 1-indexed row to resume at (default 1)
 * @return {object} {status, cells_written, next_start_row?, duration_ms}
 */
function populate_heavy_book(spreadsheet_id, sheet_name, target_rows, target_cols, resumeFromRow) {
  var t0 = Date.now();
  var MAX_RUN_MS_POPULATE = 4.0 * 60 * 1000; // 4 min safety budget
  var CHUNK_ROWS = 1000;

  var ss = SpreadsheetApp.openById(spreadsheet_id);
  var sh = ss.getSheetByName(sheet_name);
  if (!sh) {
    sh = ss.insertSheet(sheet_name);
  }

  // Expand grid if needed
  var currentRows = sh.getMaxRows();
  var currentCols = sh.getMaxColumns();
  if (target_rows > currentRows) sh.insertRowsAfter(currentRows, target_rows - currentRows);
  if (target_cols > currentCols) sh.insertColumnsAfter(currentCols, target_cols - currentCols);

  var startRow = resumeFromRow || 1;
  var cellsWritten = 0;

  for (var s = startRow; s <= target_rows; s += CHUNK_ROWS) {
    var rowsThisChunk = Math.min(CHUNK_ROWS, target_rows - s + 1);
    var data = [];
    for (var r = 0; r < rowsThisChunk; r++) {
      var row = [];
      for (var c = 0; c < target_cols; c++) {
        row.push((s + r) * 7 + c * 13);
      }
      data.push(row);
    }
    sh.getRange(s, 1, rowsThisChunk, target_cols).setValues(data);
    cellsWritten += rowsThisChunk * target_cols;

    if (Date.now() - t0 > MAX_RUN_MS_POPULATE) {
      var resume = {
        status: 'incomplete',
        cells_written: cellsWritten,
        next_start_row: s + CHUNK_ROWS,
        duration_ms: Date.now() - t0
      };
      console.log(JSON.stringify(resume));
      return resume;
    }
  }

  var done = {
    status: 'complete',
    cells_written: cellsWritten,
    total_grid_cells: target_rows * target_cols,
    duration_ms: Date.now() - t0
  };
  console.log(JSON.stringify(done));
  return done;
}


/**
 * Populate spreadsheet with EXACTLY 10-character-string values per cell.
 * Used for the 10-billion-chars test fixture: 10M cells × 10 chars = 100M chars/book.
 *
 * Each cell = 10-digit string like "0001234567" (zero-padded). Always exactly
 * 10 characters, so total chars = cells × 10 with no surprises.
 *
 * @param {string} spreadsheet_id
 * @param {string} sheet_name
 * @param {number} target_rows (e.g. 5000)
 * @param {number} target_cols (e.g. 2000)
 * @param {number=} resumeFromRow - 1-indexed row to resume at
 * @return {object} {status, cells_written, next_start_row?, duration_ms}
 */
function populate_10char_target(spreadsheet_id, sheet_name, target_rows, target_cols, resumeFromRow) {
  var t0 = Date.now();
  var MAX_RUN_MS_POPULATE = 4.0 * 60 * 1000;
  var CHUNK_ROWS = 500;  // smaller chunk than numeric — strings are heavier

  var ss = SpreadsheetApp.openById(spreadsheet_id);
  var sh = ss.getSheetByName(sheet_name);
  if (!sh) {
    sh = ss.insertSheet(sheet_name);
  }
  var currentRows = sh.getMaxRows();
  var currentCols = sh.getMaxColumns();
  if (target_rows > currentRows) sh.insertRowsAfter(currentRows, target_rows - currentRows);
  if (target_cols > currentCols) sh.insertColumnsAfter(currentCols, target_cols - currentCols);

  var startRow = resumeFromRow || 1;
  var cellsWritten = 0;

  for (var s = startRow; s <= target_rows; s += CHUNK_ROWS) {
    var rowsThisChunk = Math.min(CHUNK_ROWS, target_rows - s + 1);
    var data = [];
    for (var r = 0; r < rowsThisChunk; r++) {
      var row = [];
      for (var c = 0; c < target_cols; c++) {
        // deterministic 10-digit string, modulo 10^10 to fit
        var n = ((s + r) * 1337 + c * 7919) % 9999999999;
        var str = String(n);
        while (str.length < 10) str = '0' + str;
        row.push(str);
      }
      data.push(row);
    }
    sh.getRange(s, 1, rowsThisChunk, target_cols).setValues(data);
    cellsWritten += rowsThisChunk * target_cols;

    if (Date.now() - t0 > MAX_RUN_MS_POPULATE) {
      var resume = {
        status: 'incomplete',
        cells_written: cellsWritten,
        next_start_row: s + CHUNK_ROWS,
        duration_ms: Date.now() - t0
      };
      console.log(JSON.stringify(resume));
      return resume;
    }
  }

  var done = {
    status: 'complete',
    cells_written: cellsWritten,
    total_grid_cells: target_rows * target_cols,
    chars_per_cell: 10,
    total_chars: cellsWritten * 10,
    duration_ms: Date.now() - t0
  };
  console.log(JSON.stringify(done));
  return done;
}


// ---------- internals ----------

function _applyOp(op, processed) {
  // For 'list' op, return raw values (preserving order); for others,
  // filter to numeric.
  if (op === 'list') return processed.map(function (x) { return x.value; });
  if (op === 'count') {
    return processed.filter(function (x) {
      return typeof x.value === 'number' && !isNaN(x.value);
    }).length;
  }
  var numeric = processed
    .map(function (x) { return x.value; })
    .filter(function (v) { return typeof v === 'number' && !isNaN(v); });
  if (!numeric.length) return null;
  if (op === 'sum') return numeric.reduce(function (a, b) { return a + b; }, 0);
  if (op === 'avg') return numeric.reduce(function (a, b) { return a + b; }, 0) / numeric.length;
  if (op === 'min') return Math.min.apply(null, numeric);
  if (op === 'max') return Math.max.apply(null, numeric);
  throw new Error('unhandled op: ' + op);
}
