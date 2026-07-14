/**
 * PRDEI Journal — Docs table sync from Google Sheets
 *
 * Architecture notes:
 * - Dashboard has Category | Actual Hours | Estimate (manual) + a Sheet bar chart.
 * - Python (agent_journal) re-embeds the chart image into the Doc on each log.
 * - This Apps Script is an optional backup path for Hours/Activity/chart sync.
 * - Native Docs "Update all" on linked Sheets charts is still not available.
 *
 * -------------------------------------------------------------------------
 * SETUP
 * 1. Open https://script.google.com → New project.
 * 2. Paste this file as Code.gs.
 * 3. Deploy → New deployment → Type: Web app
 *      Execute as: Me
 *      Who has access: Anyone with Google account  (or your org)
 * 4. Copy the web app URL into Cloud Run env as DOCS_REFRESH_WEBAPP_URL
 *    (env.yaml) and redeploy the Slack agent.
 * 5. Share each project Google Doc AND its "PRDEI Activity Log — …" Sheet
 *    with the same account that owns this Apps Script (Editor).
 * -------------------------------------------------------------------------
 */

var HOURS_START = '--- HOURS SUMMARY (FROM SHEETS) ---';
var HOURS_END = '--- END HOURS SUMMARY ---';
var CHART_START = '--- CATEGORY CHART (FROM SHEETS) ---';
var CHART_END = '--- END CATEGORY CHART ---';
var ACTIVITY_START = '--- DETAILED ACTIVITY LOG ---';
var ACTIVITY_END = '--- END DETAILED ACTIVITY LOG ---';
var ACTIVITY_TAB = 'ActivityLog';
var DASHBOARD_TAB = 'Dashboard';
var CATEGORY_CHART_TITLE = 'Actual vs Estimate by Category';

function doPost(e) {
  try {
    var body = JSON.parse((e && e.postData && e.postData.contents) || '{}');
    var documentId = body.documentId;
    var spreadsheetId = body.spreadsheetId;
    var projectName = body.projectName || '';
    if (!documentId || !spreadsheetId) {
      return jsonResponse_({ ok: false, error: 'documentId and spreadsheetId required' });
    }

    var result = refreshDocFromSheet_(documentId, spreadsheetId, projectName);
    return jsonResponse_({ ok: true, result: result });
  } catch (err) {
    return jsonResponse_({ ok: false, error: String(err) });
  }
}

function doGet() {
  return jsonResponse_({
    ok: true,
    message: 'PRDEI journal Docs sync web app is running. POST {documentId, spreadsheetId}.',
  });
}

/**
 * Manual test from the Apps Script editor:
 *   refreshDocFromSheet_('DOC_ID', 'SHEET_ID', 'Tahoe Backyard');
 */
function refreshDocFromSheet_(documentId, spreadsheetId, projectName) {
  var ss = SpreadsheetApp.openById(spreadsheetId);
  var dashboard = ss.getSheetByName(DASHBOARD_TAB);
  var activity = ss.getSheetByName(ACTIVITY_TAB);
  if (!dashboard || !activity) {
    throw new Error('Spreadsheet missing Dashboard or ActivityLog tab');
  }

  var weekStart = dashboard.getRange('B1').getDisplayValue();
  var weekOf = dashboard.getRange('B2').getDisplayValue();
  var totalHours = dashboard.getRange('B3').getDisplayValue();

  // Category | Actual Hours | Estimate starts at row 5 (header) / row 6 (data).
  var categoryRows = dashboard.getRange('A6:C40').getDisplayValues()
    .filter(function (row) {
      return row[0] && String(row[0]).toLowerCase() !== 'category';
    });

  var hoursLines = [
    'Week of: ' + weekOf,
    'Week start: ' + weekStart,
    'Total Hours: ' + totalHours,
    '',
    'Hours by Category:',
  ];
  categoryRows.forEach(function (row) {
    var estimate = row[2] ? String(row[2]) : '(enter in Sheet)';
    hoursLines.push(
      '  ' + row[0] + ' ........ Actual ' + row[1] + ' hrs | Estimate ' + estimate + ' hrs'
    );
  });
  if (categoryRows.length === 0) {
    hoursLines.push('  (none this week)');
  }
  hoursLines.push('');
  hoursLines.push('(Bar chart of Actual vs Estimate appears in the section below.)');

  var activityValues = activity.getDataRange().getDisplayValues();
  var activityLines = ['Timestamp | User | Hours | Category | Activity'];
  for (var i = 1; i < activityValues.length; i++) {
    var row = activityValues[i];
    var rowWeek = row[5] || '';
    if (weekStart && rowWeek && String(rowWeek) !== String(weekStart)) {
      continue; // current-week rows only
    }
    activityLines.push(
      [row[0], row[1], row[2], row[3], row[4]].join(' | ')
    );
  }
  if (activityLines.length === 1) {
    activityLines.push('(no entries this week)');
  }

  var doc = DocumentApp.openById(documentId);
  replaceBetweenMarkers_(doc, HOURS_START, HOURS_END, hoursLines.join('\n'));
  ensureChartMarkers_(doc);
  syncCategoryChartImage_(doc, dashboard);
  replaceBetweenMarkers_(doc, ACTIVITY_START, ACTIVITY_END, activityLines.join('\n'));

  return {
    projectName: projectName,
    weekOf: weekOf,
    totalHours: totalHours,
    categoryCount: categoryRows.length,
    activityLines: activityLines.length - 1,
  };
}

function ensureChartMarkers_(doc) {
  var body = doc.getBody();
  var text = body.getText();
  if (text.indexOf(CHART_START) >= 0) {
    return;
  }
  var hoursEndSearch = body.findText(HOURS_END);
  if (!hoursEndSearch) {
    return;
  }
  var hoursEndPara = hoursEndSearch.getElement().getParent();
  var idx = body.getChildIndex(hoursEndPara);
  body.insertParagraph(idx + 1, CHART_START);
  body.insertParagraph(idx + 2, '(Actual vs Estimate chart syncs from the Sheet Dashboard.)');
  body.insertParagraph(idx + 3, CHART_END);
}

function syncCategoryChartImage_(doc, dashboard) {
  var charts = dashboard.getCharts();
  var chart = null;
  for (var i = 0; i < charts.length; i++) {
    var options = charts[i].getOptions();
    var title = '';
    try {
      title = String(options.get('title') || '');
    } catch (ignore) {}
    if (title === CATEGORY_CHART_TITLE || !chart) {
      chart = charts[i];
      if (title === CATEGORY_CHART_TITLE) {
        break;
      }
    }
  }
  if (!chart) {
    return;
  }

  // Export via Slides so the PNG matches the Sheet chart styling.
  var temp = SlidesApp.create('PRDEI temp chart export');
  var slide = temp.getSlides()[0];
  var imageBlob = slide.insertSheetsChartAsImage(chart).getAs('image/png');
  DriveApp.getFileById(temp.getId()).setTrashed(true);

  // Clear content between chart markers, then insert image.
  var body = doc.getBody();
  var collecting = false;
  var toDelete = [];
  var insertAfter = null;
  for (var j = 0; j < body.getNumChildren(); j++) {
    var child = body.getChild(j);
    var childText = '';
    try {
      childText = child.asParagraph().getText();
    } catch (ignore2) {
      childText = '';
    }
    if (childText.indexOf(CHART_START) >= 0) {
      collecting = true;
      insertAfter = child;
      continue;
    }
    if (collecting && childText.indexOf(CHART_END) >= 0) {
      break;
    }
    if (collecting) {
      toDelete.push(child);
    }
  }
  for (var d = toDelete.length - 1; d >= 0; d--) {
    toDelete[d].removeFromParent();
  }
  if (insertAfter) {
    var para = body.insertParagraph(body.getChildIndex(insertAfter) + 1, '');
    para.appendInlineImage(imageBlob).setWidth(480).setHeight(280);
  }
}

function replaceBetweenMarkers_(doc, startMarker, endMarker, replacementText) {
  var body = doc.getBody();
  var text = body.getText();
  var start = text.indexOf(startMarker);
  var end = text.indexOf(endMarker);
  if (start < 0 || end < 0 || end <= start) {
    throw new Error('Markers not found: ' + startMarker + ' / ' + endMarker);
  }

  // Body find/replace by deleting the range between markers and inserting fresh text.
  var rangeStart = start + startMarker.length;
  var search = body.findText(startMarker);
  if (!search) {
    throw new Error('Could not locate start marker element: ' + startMarker);
  }
  var startEl = search.getElement();

  // Prefer a paragraph-based rewrite for reliability.
  var startPara = startEl.getParent();
  var collecting = false;
  var toDelete = [];
  for (var i = 0; i < body.getNumChildren(); i++) {
    var child = body.getChild(i);
    var childText = '';
    try {
      childText = child.asText().getText();
    } catch (ignore) {
      try {
        childText = child.asParagraph().getText();
      } catch (ignore2) {
        childText = '';
      }
    }
    if (childText.indexOf(startMarker) >= 0) {
      collecting = true;
      continue;
    }
    if (collecting && childText.indexOf(endMarker) >= 0) {
      break;
    }
    if (collecting) {
      toDelete.push(child);
    }
  }
  for (var d = toDelete.length - 1; d >= 0; d--) {
    toDelete[d].removeFromParent();
  }

  // Insert replacement immediately after the start-marker paragraph.
  var insertAfter = null;
  for (var j = 0; j < body.getNumChildren(); j++) {
    var c = body.getChild(j);
    var t = '';
    try {
      t = c.asParagraph().getText();
    } catch (ignore3) {}
    if (t.indexOf(startMarker) >= 0) {
      insertAfter = c;
      break;
    }
  }
  if (!insertAfter) {
    body.appendParagraph(replacementText);
    return;
  }

  var lines = String(replacementText || '').split('\n');
  var cursor = insertAfter;
  for (var k = 0; k < lines.length; k++) {
    cursor = body.insertParagraph(body.getChildIndex(cursor) + 1, lines[k]);
  }
}

function jsonResponse_(obj) {
  return ContentService
    .createTextOutput(JSON.stringify(obj))
    .setMimeType(ContentService.MimeType.JSON);
}
