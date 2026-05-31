const SHEET_NAME = "待发名单";
const SENDER_EMAIL = "seraphinaleclair0@gmail.com";
const GITHUB_OWNER = "seraphinaleclair0-cmyk";
const GITHUB_REPO = "zdeer-v2";
const GITHUB_WORKFLOW_ID = "send-outreach.yml";
const GITHUB_REF = "main";

const COL_CREATOR = 1; // A
const COL_EMAIL = 2; // B
const COL_SENDER = 4; // D
const COL_SEND = 5; // E
const COL_FOLLOWUP = 6; // F
const COL_DEVELOPED_AT = 7; // G
const COL_RATING = 16; // P

function doPost(e) {
  try {
    const payload = e && e.postData && e.postData.contents
      ? JSON.parse(e.postData.contents)
      : {};

    const email = normalizeEmail(payload.email);
    const creator = normalizeCreator(payload.creator);

    if (!email || !/^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$/.test(email)) {
      return jsonResponse({
        ok: false,
        message: "❌ 邮箱识别失败",
      });
    }

    const spreadsheet = SpreadsheetApp.getActiveSpreadsheet();
    const sheet = spreadsheet.getSheetByName(SHEET_NAME);
    if (!sheet) {
      throw new Error(`找不到工作表：${SHEET_NAME}`);
    }

    const result = upsertCreatorRow(sheet, email, creator);
    const githubResult = triggerGitHubWorkflow(email, creator);

    if (!githubResult.ok) {
      return jsonResponse({
        ok: false,
        type: result.type,
        message: `❌ 已写表，但 GitHub 触发失败：${githubResult.message}`,
      });
    }

    return jsonResponse({
      ok: true,
      type: result.type,
      message: result.type === "new"
        ? "✅ 已写入并触发发信"
        : "✅ 已更新并触发跟进",
    });
  } catch (error) {
    return jsonResponse({
      ok: false,
      message: `❌ 失败：${error.message}`,
    });
  }
}

function normalizeEmail(email) {
  return String(email || "")
    .toLowerCase()
    .replace(/\s+/g, "")
    .trim();
}

function normalizeCreator(creator) {
  return String(creator || "")
    .trim()
    .replace(/^@+/, "")
    .replace(/\s+/g, "");
}

function findRowByEmail(sheet, email) {
  const normalizedEmail = normalizeEmail(email);
  const lastRow = sheet.getLastRow();

  if (!normalizedEmail || lastRow < 2) {
    return 0;
  }

  const values = sheet.getRange(2, COL_EMAIL, lastRow - 1, 1).getValues();
  for (let index = 0; index < values.length; index += 1) {
    if (normalizeEmail(values[index][0]) === normalizedEmail) {
      return index + 2;
    }
  }

  return 0;
}

function upsertCreatorRow(sheet, email, creator) {
  const row = findRowByEmail(sheet, email);
  const now = new Date();

  if (row) {
    const currentCreator = String(sheet.getRange(row, COL_CREATOR).getValue() || "").trim();
    const followupValue = sheet.getRange(row, COL_FOLLOWUP).getValue();
    const followupCount = Number(followupValue) || 0;

    if (!currentCreator && creator) {
      sheet.getRange(row, COL_CREATOR).setValue(creator);
    }

    sheet.getRange(row, COL_SENDER, 1, 4).setValues([[
      SENDER_EMAIL,
      "✅",
      followupCount + 1,
      now,
    ]]);
    sheet.getRange(row, COL_RATING).setValue("S");

    return {
      type: "existing",
      row,
    };
  }

  const newRow = Math.max(sheet.getLastRow() + 1, 2);
  sheet.getRange(newRow, COL_CREATOR, 1, 2).setValues([[creator, email]]);
  sheet.getRange(newRow, COL_SENDER, 1, 4).setValues([[
    SENDER_EMAIL,
    "✅",
    0,
    now,
  ]]);
  sheet.getRange(newRow, COL_RATING).setValue("S");

  return {
    type: "new",
    row: newRow,
  };
}

function triggerGitHubWorkflow(email, creator) {
  const token = PropertiesService.getScriptProperties().getProperty("GITHUB_TOKEN");
  if (!token) {
    return {
      ok: false,
      message: "Script Properties 缺少 GITHUB_TOKEN",
    };
  }

  const url = `https://api.github.com/repos/${GITHUB_OWNER}/${GITHUB_REPO}/actions/workflows/${GITHUB_WORKFLOW_ID}/dispatches`;
  const response = UrlFetchApp.fetch(url, {
    method: "post",
    muteHttpExceptions: true,
    contentType: "application/json",
    headers: {
      Accept: "application/vnd.github+json",
      Authorization: `Bearer ${token}`,
      "X-GitHub-Api-Version": "2022-11-28",
    },
    payload: JSON.stringify({
      ref: GITHUB_REF,
      inputs: {
        source: "iphone_ocr",
        email,
        creator,
      },
    }),
  });

  const status = response.getResponseCode();
  if (status >= 200 && status < 300) {
    return {
      ok: true,
    };
  }

  return {
    ok: false,
    message: `HTTP ${status} ${response.getContentText()}`,
  };
}

function jsonResponse(obj) {
  return ContentService
    .createTextOutput(JSON.stringify(obj))
    .setMimeType(ContentService.MimeType.JSON);
}
