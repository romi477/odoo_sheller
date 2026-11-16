// Colour for the snippets in Settings, and nothing more. Three tiny grammars —
// the JSON, TOML and ssh_config this app itself writes — rather than a
// highlighting library in the bundle. Every function returns [kind, text]
// pairs whose texts join back into the input exactly: the sheet shows what the
// copy button copies, character for character. Kind "" is plain text.

// A double-quoted string, with backslash escapes, as JSON and TOML both spell it.
const QUOTED = /"(?:[^"\\\n]|\\.)*"/y;

function scan(text, rules) {
  const out = [];
  let at = 0;
  let plain = "";
  while (at < text.length) {
    let matched = false;
    for (const [kind, pattern, classify] of rules) {
      pattern.lastIndex = at;
      const found = pattern.exec(text);
      if (found && found[0].length) {
        if (plain) out.push(["", plain]);
        plain = "";
        out.push([classify ? classify(text, at + found[0].length) : kind, found[0]]);
        at += found[0].length;
        matched = true;
        break;
      }
    }
    if (!matched) {
      plain += text[at];
      at += 1;
    }
  }
  if (plain) out.push(["", plain]);
  return out;
}

// A JSON string is a key when the next thing after it is a colon.
function keyOrString(text, end) {
  return /^\s*:/.test(text.slice(end)) ? "key" : "string";
}

const GRAMMARS = {
  json: (text) =>
    scan(text, [
      ["string", QUOTED, keyOrString],
      ["punct", /[{}\[\]:,]/y],
      ["literal", /\b(?:true|false|null|-?\d+(?:\.\d+)?)\b/y],
    ]),
  toml: (text) =>
    scan(text, [
      ["section", /^\[[^\]\n]*\]/my],
      ["key", /^[A-Za-z0-9_.-]+(?=\s*=)/my],
      ["string", QUOTED],
      ["punct", /[\[\],=]/y],
      ["comment", /#[^\n]*/y],
    ]),
  ssh: (text) =>
    scan(text, [
      ["comment", /(?<=^[ \t]*)#[^\n]*/my],
      ["key", /(?<=^[ \t]*)[A-Za-z]+(?=[ \t])/my],
      ["string", /(?<=^[ \t]*[A-Za-z]+[ \t]+)[^\n]+/my],
    ]),
};

function highlight(text, lang) {
  const grammar = GRAMMARS[lang];
  return grammar ? grammar(text) : [["", text]];
}

if (typeof module !== "undefined") module.exports = { highlight };
