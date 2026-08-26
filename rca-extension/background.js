/* background.js — Service Worker */

// Open side panel on toolbar icon click (keeps it open, won't close on blur)
chrome.action.onClicked.addListener(tab => {
  chrome.sidePanel.open({ windowId: tab.windowId });
});

chrome.runtime.onMessage.addListener((request, sender, sendResponse) => {
  handleMessage(request).then(data => sendResponse({ data })).catch(err => sendResponse({ error: err.message }));
  return true; // Keep channel open for async response
});

async function handleMessage(request) {
  switch (request.type) {
    case 'FETCH_SLACK':   return fetchSlackData(request.caseNumber);
    case 'FETCH_ORGCS':   return fetchOrgCSData(request.caseNumber);
    case 'FETCH_ORG62':   return fetchOrg62Data(request.caseNumber);
    case 'FETCH_PUBLIC':  return fetchPublicData(request.caseNumber);
    default: throw new Error('Unknown message type: ' + request.type);
  }
}

/* ── Slack Data ──
   Searches SEV channels and swarm threads for the case number.
   In production: replace with your internal Slack API integration or MCP proxy URL.
*/
async function fetchSlackData(caseNumber) {
  try {
    const headers = await getSlackHeaders();
    if (!headers) return { note: 'Slack token not configured — add SLACK_TOKEN in extension settings', channels: [], threads: [] };

    const [channelResults, threadResults] = await Promise.allSettled([
      searchSlackChannels(caseNumber, headers),
      searchSlackThreads(caseNumber, headers),
    ]);

    return {
      channels: channelResults.status === 'fulfilled' ? channelResults.value : [],
      threads: threadResults.status === 'fulfilled' ? threadResults.value : [],
    };
  } catch (err) {
    return { note: 'Slack fetch error: ' + err.message, channels: [], threads: [] };
  }
}

async function getSlackHeaders() {
  const { slackToken } = await chromeGet('slackToken');
  if (!slackToken) return null;
  return { Authorization: `Bearer ${slackToken}`, 'Content-Type': 'application/json' };
}

async function searchSlackChannels(caseNumber, headers) {
  // Search for SEV channels mentioning this case
  const query = encodeURIComponent(`${caseNumber} in:#sev`);
  const res = await fetch(`https://slack.com/api/search.messages?query=${query}&count=20`, { headers });
  const json = await res.json();
  if (!json.ok) return [];
  return (json.messages?.matches || []).map(m => ({
    channel: m.channel?.name,
    user: m.username,
    text: m.text,
    ts: m.ts,
    permalink: m.permalink,
  }));
}

async function searchSlackThreads(caseNumber, headers) {
  // Search for swarm threads
  const query = encodeURIComponent(`${caseNumber} swarm`);
  const res = await fetch(`https://slack.com/api/search.messages?query=${query}&count=20`, { headers });
  const json = await res.json();
  if (!json.ok) return [];
  return (json.messages?.matches || []).map(m => ({
    channel: m.channel?.name,
    user: m.username,
    text: m.text,
    ts: m.ts,
    permalink: m.permalink,
  }));
}

/* ── OrgCS Data ──
   Queries Salesforce OrgCS for case details.
   In production: replace endpoint with your Salesforce Connected App OAuth flow or MCP proxy.
*/
async function fetchOrgCSData(caseNumber) {
  try {
    const { orgcsToken, orgcsInstanceUrl } = await chromeGet(['orgcsToken', 'orgcsInstanceUrl']);
    if (!orgcsToken || !orgcsInstanceUrl) {
      return { note: 'OrgCS credentials not configured', caseData: null };
    }

    const soql = encodeURIComponent(
      `SELECT Id, CaseNumber, Subject, Description, Status, Priority, Account.Name, Account.Id, CreatedDate, ClosedDate, Origin, Type, OwnerId, Owner.Name FROM Case WHERE CaseNumber = '${caseNumber}' LIMIT 1`
    );

    const res = await fetch(`${orgcsInstanceUrl}/services/data/v60.0/query?q=${soql}`, {
      headers: {
        Authorization: `Bearer ${orgcsToken}`,
        'Content-Type': 'application/json',
      },
    });

    if (!res.ok) throw new Error(`OrgCS API ${res.status}`);
    const json = await res.json();
    const record = json.records?.[0] || null;

    if (!record) return { note: `No case found for ${caseNumber}`, caseData: null };

    return {
      caseData: {
        id: record.Id,
        caseNumber: record.CaseNumber,
        subject: record.Subject,
        description: record.Description,
        status: record.Status,
        priority: record.Priority,
        accountName: record.Account?.Name,
        accountId: record.Account?.Id,
        createdDate: record.CreatedDate,
        closedDate: record.ClosedDate,
        origin: record.Origin,
        type: record.Type,
        owner: record.Owner?.Name,
      }
    };
  } catch (err) {
    return { note: 'OrgCS fetch error: ' + err.message, caseData: null };
  }
}

/* ── Org62 Data ──
   Queries internal Salesforce Org62 for account / success plan / escalation data.
*/
async function fetchOrg62Data(caseNumber) {
  try {
    const { org62Token, org62InstanceUrl } = await chromeGet(['org62Token', 'org62InstanceUrl']);
    if (!org62Token || !org62InstanceUrl) {
      return { note: 'Org62 credentials not configured', accountData: null };
    }

    // First find the account from case number via Org62
    const soql = encodeURIComponent(
      `SELECT Id, Name, Industry, BillingCountry, Support_Level__c, Is_Red_Account__c FROM Account WHERE Id IN (SELECT AccountId FROM Case WHERE CaseNumber = '${caseNumber}') LIMIT 1`
    );

    const res = await fetch(`${org62InstanceUrl}/services/data/v60.0/query?q=${soql}`, {
      headers: {
        Authorization: `Bearer ${org62Token}`,
        'Content-Type': 'application/json',
      },
    });

    if (!res.ok) throw new Error(`Org62 API ${res.status}`);
    const json = await res.json();
    const record = json.records?.[0] || null;

    if (!record) return { note: 'No Org62 account found for this case', accountData: null };

    return {
      accountData: {
        id: record.Id,
        name: record.Name,
        industry: record.Industry,
        country: record.BillingCountry,
        supportLevel: record.Support_Level__c,
        isRedAccount: record.Is_Red_Account__c,
      }
    };
  } catch (err) {
    return { note: 'Org62 fetch error: ' + err.message, accountData: null };
  }
}

/* ── Public Data ──
   Searches Salesforce Help & Known Issues (public web).
   Uses Salesforce public search endpoints — no auth required.
*/
async function fetchPublicData(caseNumber) {
  try {
    const results = await Promise.allSettled([
      searchKnownIssues(caseNumber),
      searchHelpArticles(caseNumber),
    ]);

    return {
      knownIssues: results[0].status === 'fulfilled' ? results[0].value : [],
      helpArticles: results[1].status === 'fulfilled' ? results[1].value : [],
    };
  } catch (err) {
    return { note: 'Public search error: ' + err.message, knownIssues: [], helpArticles: [] };
  }
}

async function searchKnownIssues(caseNumber) {
  const res = await fetch(
    `https://status.salesforce.com/api/v1/incidents?limit=10`,
    { headers: { 'Accept': 'application/json' } }
  );
  if (!res.ok) return [];
  const json = await res.json();
  return (json || []).slice(0, 5).map(i => ({
    title: i.message,
    id: i.id,
    startTime: i.startTime,
    endTime: i.endTime,
    status: i.status,
    source: 'Salesforce Status Page',
  }));
}

async function searchHelpArticles(caseNumber) {
  // Salesforce Help public search
  const query = encodeURIComponent(caseNumber);
  const res = await fetch(
    `https://help.salesforce.com/services/search/suggestTitleMatch?q=${query}&language=en_US&limit=5`,
    { headers: { 'Accept': 'application/json' } }
  );
  if (!res.ok) return [];
  const json = await res.json().catch(() => null);
  if (!json?.autoSuggestItems) return [];
  return json.autoSuggestItems.map(a => ({
    title: a.title,
    url: a.url,
    summary: a.summary,
    source: 'Salesforce Help',
  }));
}

/* ── Utility ── */
function chromeGet(keys) {
  return new Promise(resolve => chrome.storage.local.get(keys, resolve));
}
