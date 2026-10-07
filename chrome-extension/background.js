chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message.type !== "download") return;

  chrome.downloads.download({
    url: message.url,
    filename: message.filename,
    saveAs: true,
    conflictAction: "uniquify"
  }).then((downloadId) => sendResponse({ ok: true, downloadId }))
    .catch((error) => sendResponse({ ok: false, error: error.message }));
  return true;
});
