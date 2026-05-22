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
      return {
        status: 'incomplete',
        token: token,
        processed_count: processed.length,
        errors_count: errors.length,
        remaining_count: ids.length - i,
        _meta: { op: op, sheet: sheet, cell: cell, elapsed_ms: Date.now() - t0 }
      };
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

  return {
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
}


/**
 * Inspect an incomplete run's saved state without resuming.
 * @param {string} token - resume token returned by a prior cross_aggregate
 * @return {object} {status, processed_count, remaining_count} or {status:"not_found"}
 */
function cross_aggregate_status(token) {
  var cache = CacheService.getScriptCache();
  var saved = cache.get(token);
  if (!saved) return { status: 'not_found', token: token };
  var state = JSON.parse(saved);
  return {
    status: 'incomplete',
    token: token,
    processed_count: (state.processed || []).length,
    errors_count: (state.errors || []).length,
    remaining_count: (state.ids || []).length - (state.startIndex || 0),
    _meta: { op: state.op, sheet: state.sheet, cell: state.cell }
  };
}


/**
 * Trivial health probe — proves the Apps Script project is deployed and
 * reachable via the Execution API. Used by scripts/verify_phase14_setup.py.
 * @return {object} {ok: true, version: "phase14", runtime: "V8"}
 */
function ping() {
  return { ok: true, version: 'phase14', runtime: 'V8', server_time: new Date().toISOString() };
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
