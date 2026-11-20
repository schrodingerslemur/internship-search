/* Fill it myself: run on an application form in the user's own browser.
   Fills what it can match from the answers Internship Search drafted, never
   submits, and lists what is left (files, CAPTCHA, unmatched questions).
   The answers arrive in the URL fragment (#isfill=...) or, failing that, are
   pasted from the clipboard. Served as a bookmarklet by app/web/routes/autoapply.py. */
(function () {
  var KEY = "isfill=";
  function decode(code) {
    code = code.replace(/-/g, "+").replace(/_/g, "/");
    while (code.length % 4) code += "=";
    var bin = atob(code), bytes = new Uint8Array(bin.length);
    for (var i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
    return JSON.parse(new TextDecoder().decode(bytes));
  }
  var data = null, hash = location.hash || "", at = hash.indexOf(KEY);
  try {
    if (at >= 0) {
      data = decode(hash.slice(at + KEY.length).split("&")[0]);
      /* Keep the answers out of the address bar and history. */
      history.replaceState(null, "", location.pathname + location.search);
    } else {
      var pasted = prompt("Paste the answers code that Internship Search copied for you:");
      if (!pasted) return;
      data = decode(pasted.trim().replace(/^.*isfill=/, ""));
    }
  } catch (e) {
    alert("Internship Search: that answers code could not be read. Press Fill it myself again.");
    return;
  }

  function norm(s) {
    return (s || "").toLowerCase().replace(/[’‘]/g, "'").replace(/\*/g, "")
      .replace(/\(required\)|\brequired\b/g, "").replace(/[^a-z0-9' ]+/g, " ").replace(/\s+/g, " ").trim();
  }
  function text(el) { return el ? (el.innerText || el.textContent || "") : ""; }
  function labelOf(el) {
    var doc = el.ownerDocument, s = "";
    if (el.id) {
      var l = doc.querySelector('label[for="' + (window.CSS ? CSS.escape(el.id) : el.id) + '"]');
      if (l) s = text(l);
    }
    if (!s && el.getAttribute("aria-labelledby")) {
      s = el.getAttribute("aria-labelledby").split(/\s+/).map(function (id) {
        return text(doc.getElementById(id));
      }).join(" ");
    }
    if (!s) s = el.getAttribute("aria-label") || "";
    if (!s && el.closest("label")) s = text(el.closest("label"));
    if (!s) {
      var box = el.closest("fieldset, [role=group], .field, .application-question, li, div");
      var legend = box && box.querySelector("legend, label, .application-label, .text");
      if (legend) s = text(legend);
    }
    return s || el.getAttribute("placeholder") || el.name || "";
  }

  /* Profile fields recognised by what the input is called. */
  var p = data.profile || {};
  var byKind = [
    [/first.?name|given.?name|fname/, p.first_name],
    [/last.?name|family.?name|surname|lname/, p.last_name],
    [/^name$|full.?name|your name/, [p.first_name, p.last_name].filter(Boolean).join(" ")],
    [/e.?mail/, p.email],
    [/phone|mobile|tel/, p.phone],
    [/linkedin/, p.linkedin],
    [/github/, p.github],
    [/website|portfolio|personal site/, p.website],
    [/^location$|current location|city/, p.location]
  ];
  var answers = (data.answers || []).filter(function (a) { return a.value !== "" && a.value != null; });
  var used = {};
  function answerFor(label) {
    var n = norm(label);
    if (!n) return null;
    for (var i = 0; i < answers.length; i++) if (norm(answers[i].label) === n) return i;
    for (var j = 0; j < answers.length; j++) {
      var m = norm(answers[j].label);
      if (m.length >= 6 && n.length >= 6 && (n.indexOf(m) >= 0 || m.indexOf(n) >= 0)) return j;
    }
    return null;
  }
  function setValue(el, value) {
    var proto = el.tagName === "TEXTAREA" ? HTMLTextAreaElement.prototype
      : el.tagName === "SELECT" ? HTMLSelectElement.prototype : HTMLInputElement.prototype;
    var setter = Object.getOwnPropertyDescriptor(proto, "value").set;
    setter.call(el, value);
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
    el.dispatchEvent(new Event("blur", { bubbles: true }));
  }
  function pickOption(select, value) {
    var want = norm(Array.isArray(value) ? value[0] : value), best = null;
    for (var i = 0; i < select.options.length; i++) {
      var o = norm(select.options[i].text);
      if (o === want) { best = select.options[i]; break; }
      if (!best && want && o && (o.indexOf(want) === 0 || want.indexOf(o) === 0)) best = select.options[i];
    }
    if (best) { setValue(select, best.value); return true; }
    return false;
  }

  var docs = [document];
  Array.prototype.forEach.call(document.querySelectorAll("iframe"), function (f) {
    try { if (f.contentDocument) docs.push(f.contentDocument); } catch (e) { /* another site's frame */ }
  });

  var filled = 0, files = [];
  docs.forEach(function (doc) {
    var fields = doc.querySelectorAll("input, textarea, select");
    Array.prototype.forEach.call(fields, function (el) {
      var type = (el.type || "").toLowerCase();
      if (el.disabled || el.readOnly || /hidden|submit|button|reset|image|password/.test(type)) return;
      if (el.offsetParent === null && type !== "radio" && type !== "checkbox") return;
      var label = labelOf(el);
      if (type === "file") { files.push(label || "a file"); return; }
      if (type === "radio" || type === "checkbox") {
        var group = el.closest("fieldset, [role=radiogroup], [role=group], .field, li, div");
        var question = group ? text(group.querySelector("legend, label, .application-label")) : "";
        var gi = answerFor(question);
        if (gi === null) return;
        var opt = norm(text(el.closest("label")) || labelOf(el) || el.value);
        var want = [].concat(answers[gi].value).map(norm);
        if (want.indexOf(opt) >= 0 && !el.checked) { el.click(); filled++; used[gi] = 1; }
        return;
      }
      if (el.value && el.tagName !== "SELECT") return; /* never overwrite what is there */
      var i = answerFor(label), value = null;
      if (i !== null) { value = answers[i].value; }
      else {
        var hint = norm([label, el.name, el.id, el.getAttribute("autocomplete")].join(" "));
        for (var k = 0; k < byKind.length; k++) {
          if (byKind[k][1] && byKind[k][0].test(hint)) { value = byKind[k][1]; break; }
        }
      }
      if (value == null || value === "") return;
      if (el.tagName === "SELECT") {
        if (pickOption(el, value)) { filled++; if (i !== null) used[i] = 1; }
      } else {
        setValue(el, Array.isArray(value) ? value.join(", ") : String(value));
        filled++;
        if (i !== null) used[i] = 1;
      }
    });
  });

  var left = answers.filter(function (a, i) { return !used[i]; });
  var ask = data.ask || [];
  var old = document.getElementById("isfill-panel");
  if (old) old.remove();
  var panel = document.createElement("div");
  panel.id = "isfill-panel";
  panel.style.cssText = "position:fixed;right:16px;bottom:16px;z-index:2147483647;max-width:380px;max-height:70vh;" +
    "overflow:auto;background:#fff;color:#111;border:2px solid #1a5fb4;border-radius:12px;padding:14px 16px;" +
    "font:14px/1.45 -apple-system,Segoe UI,Roboto,sans-serif;box-shadow:0 8px 30px rgba(0,0,0,.25)";
  function add(tag, content, css) {
    var el = document.createElement(tag);
    el.textContent = content;
    if (css) el.style.cssText = css;
    panel.appendChild(el);
    return el;
  }
  add("div", "Internship Search filled " + filled + " field" + (filled === 1 ? "" : "s"), "font-weight:700;margin-bottom:6px");
  add("div", "Check every field, then solve the CAPTCHA and press Submit yourself. Nothing was submitted.",
      "color:#444;margin-bottom:8px");
  if (files.length) add("div", "Upload yourself: " + files.join("; "), "margin-bottom:8px;color:#a15c00");
  if (ask.length) add("div", "Still yours to answer: " + ask.join("; "), "margin-bottom:8px;color:#a15c00");
  if (left.length) {
    add("div", "Not placed (click to copy, then paste):", "font-weight:600;margin:6px 0 4px");
    left.forEach(function (a) {
      var row = add("button", a.label + ": " + [].concat(a.value).join(", "),
        "display:block;width:100%;text-align:left;margin:3px 0;padding:6px 8px;border:1px solid #ccc;" +
        "border-radius:6px;background:#f6f7f9;color:#111;cursor:pointer;font:inherit;white-space:pre-wrap");
      row.onclick = function () {
        navigator.clipboard.writeText([].concat(a.value).join(", "));
        row.style.background = "#e3f4e6";
      };
    });
  }
  var close = add("button", "Close", "margin-top:8px;padding:5px 12px;border:1px solid #1a5fb4;border-radius:6px;" +
    "background:#1a5fb4;color:#fff;cursor:pointer;font:inherit");
  close.onclick = function () { panel.remove(); };
  document.body.appendChild(panel);
})();
