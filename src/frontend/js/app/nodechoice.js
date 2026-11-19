// nodechoice.js - shared text for the "backend or node" choice
// --------------------------------------------------------------------
// Entries from api.nodeChoices(module): {id, name, installed, active, outdated}.
// A node is selectable only if the node module is installed there AND enabled
// individually (since node 0.3.0 every module is off after installing).
export function nodeHint(n) {
  if (n.active) return "";
  if (n.outdated) return " (node outdated - update first)";
  return n.installed ? " (module not enabled)" : " (module not installed)";
}
