// crypto.js (module "passwords") - end-to-end encryption in the browser.
// -----------------------------------------------------------------------
// Zero-knowledge: the master password, derived key, private key, vault keys and
// plaintext NEVER leave the browser. The backend stores ciphertext only. Uses
// WebCrypto exclusively (no third-party libraries).
//
// Design (Bitwarden/KeePass principle):
//   master password --PBKDF2--> userKey (AES-256)        [in memory only]
//   userKey encrypts:  the RSA private key + a verifier
//   RSA public key: stored in plaintext (for sharing)
//   each vault has a random vaultKey (AES-256-GCM),
//     wrapped per member with their RSA public key (wrapped_key)
//   entries: fully encrypted with the vaultKey (AES-GCM)
//
// This is "the encrypted master key that gets stored": the private key lives in
// the backend only as a blob encrypted with the master password.

const te = new TextEncoder();
const td = new TextDecoder();
const b64 = (buf) => btoa(String.fromCharCode(...new Uint8Array(buf)));
const unb64 = (s) => Uint8Array.from(atob(s), (c) => c.charCodeAt(0));
const PBKDF2_ITERS = 600000;

export const cryptoReady = !!(window.crypto && window.crypto.subtle);

// ---------------------------------------------------------------- PBKDF2 -> userKey
async function deriveUserKey(masterPassword, saltB64, iters = PBKDF2_ITERS) {
  const base = await crypto.subtle.importKey("raw", te.encode(masterPassword), "PBKDF2", false, ["deriveKey"]);
  return crypto.subtle.deriveKey(
    { name: "PBKDF2", salt: unb64(saltB64), iterations: iters, hash: "SHA-256" },
    base, { name: "AES-GCM", length: 256 }, false, ["encrypt", "decrypt", "wrapKey", "unwrapKey"]);
}

// ---------------------------------------------------------------- AES-GCM
async function aesEnc(key, plaintextBytes) {
  const iv = crypto.getRandomValues(new Uint8Array(12));
  const ct = await crypto.subtle.encrypt({ name: "AES-GCM", iv }, key, plaintextBytes);
  return b64(iv) + ":" + b64(ct);
}
async function aesDec(key, packed) {
  const [ivB, ctB] = packed.split(":");
  const pt = await crypto.subtle.decrypt({ name: "AES-GCM", iv: unb64(ivB) }, key, unb64(ctB));
  return new Uint8Array(pt);
}
export const encText = async (key, s) => aesEnc(key, te.encode(s));
export const decText = async (key, packed) => td.decode(await aesDec(key, packed));
export const encJson = (key, obj) => encText(key, JSON.stringify(obj));
export const decJson = async (key, packed) => JSON.parse(await decText(key, packed));

// ---------------------------------------------------------------- Create account
// Generates salt, RSA key pair, encrypted private key, verifier.
export async function createAccount(masterPassword) {
  const salt = crypto.getRandomValues(new Uint8Array(16));
  const saltB64 = b64(salt);
  const userKey = await deriveUserKey(masterPassword, saltB64);
  const kp = await crypto.subtle.generateKey(
    { name: "RSA-OAEP", modulusLength: 2048, publicExponent: new Uint8Array([1, 0, 1]), hash: "SHA-256" },
    true, ["wrapKey", "unwrapKey"]);
  const pub = b64(await crypto.subtle.exportKey("spki", kp.publicKey));
  const privPkcs8 = await crypto.subtle.exportKey("pkcs8", kp.privateKey);
  const encPriv = await aesEnc(userKey, privPkcs8);
  const verifier = await encText(userKey, "BYTERA-PM-OK");
  return { salt: saltB64, iters: PBKDF2_ITERS, public_key: pub, enc_private_key: encPriv, verifier,
           _session: { userKey, privateKey: kp.privateKey, publicKey: pub } };
}

// ---------------------------------------------------------------- Unlock
export async function unlock(masterPassword, account) {
  const userKey = await deriveUserKey(masterPassword, account.salt, account.iters || PBKDF2_ITERS);
  try {
    const v = await decText(userKey, account.verifier);
    if (v !== "BYTERA-PM-OK") throw new Error("bad");
  } catch {
    throw new Error("Wrong master password");
  }
  const privBytes = await aesDec(userKey, account.enc_private_key);
  const privateKey = await crypto.subtle.importKey("pkcs8", privBytes,
    { name: "RSA-OAEP", hash: "SHA-256" }, true, ["unwrapKey", "decrypt"]);
  return { userKey, privateKey, publicKey: account.public_key };
}

// ---------------------------------------------------------------- Change master password
// Only the blob with the private key + verifier are re-encrypted; vault keys stay
// untouched (they hang off the RSA key, not the master password).
export async function rekey(session, newMasterPassword) {
  const salt = crypto.getRandomValues(new Uint8Array(16));
  const saltB64 = b64(salt);
  const userKey = await deriveUserKey(newMasterPassword, saltB64);
  const privPkcs8 = await crypto.subtle.exportKey("pkcs8", session.privateKey);
  return { salt: saltB64, iters: PBKDF2_ITERS,
           enc_private_key: await aesEnc(userKey, privPkcs8),
           verifier: await encText(userKey, "BYTERA-PM-OK"),
           _userKey: userKey };
}

// ---------------------------------------------------------------- Vault keys
export async function newVaultKey() {
  return crypto.subtle.generateKey({ name: "AES-GCM", length: 256 }, true, ["encrypt", "decrypt"]);
}
// wrap the vaultKey with an RSA public key (spki, base64) -> wrapped_key
export async function wrapVaultKey(vaultKey, publicKeyB64) {
  const pub = await crypto.subtle.importKey("spki", unb64(publicKeyB64), { name: "RSA-OAEP", hash: "SHA-256" }, false, ["wrapKey"]);
  const wrapped = await crypto.subtle.wrapKey("raw", vaultKey, pub, { name: "RSA-OAEP" });
  return b64(wrapped);
}
// unwrap the wrapped_key with your own private key -> vaultKey
export async function unwrapVaultKey(wrappedB64, privateKey) {
  return crypto.subtle.unwrapKey("raw", unb64(wrappedB64), privateKey, { name: "RSA-OAEP" },
    { name: "AES-GCM", length: 256 }, true, ["encrypt", "decrypt"]);
}
// export/import the raw vaultKey (for import/export to the backend, base64)
export const exportVaultKeyRaw = async (vaultKey) => b64(await crypto.subtle.exportKey("raw", vaultKey));
export const importVaultKeyRaw = async (rawB64) =>
  crypto.subtle.importKey("raw", unb64(rawB64), { name: "AES-GCM", length: 256 }, true, ["encrypt", "decrypt"]);

// ---------------------------------------------------------------- Password generator
const SETS = { lower: "abcdefghijkmnpqrstuvwxyz", upper: "ABCDEFGHJKLMNPQRSTUVWXYZ",
               digits: "23456789", symbols: "!@#$%^&*()-_=+[]{};:,.?", ambiguous: "il1Lo0O" };
export function generatePassword(opts = {}) {
  const o = { length: 20, lower: true, upper: true, digits: true, symbols: true, avoidAmbiguous: true, ...opts };
  let pool = "";
  const req = [];
  for (const k of ["lower", "upper", "digits", "symbols"]) {
    if (!o[k]) continue;
    let set = SETS[k];
    if (!o.avoidAmbiguous) set += (k === "lower" ? "lo" : k === "upper" ? "ILO" : k === "digits" ? "01" : "");
    pool += set; req.push(set);
  }
  if (!pool) pool = SETS.lower;
  const pick = (set) => set[crypto.getRandomValues(new Uint32Array(1))[0] % set.length];
  const out = req.map(pick);                    // at least one char from each group
  for (let i = out.length; i < o.length; i++) out.push(pick(pool));
  for (let i = out.length - 1; i > 0; i--) {    // Fisher-Yates
    const j = crypto.getRandomValues(new Uint32Array(1))[0] % (i + 1);
    [out[i], out[j]] = [out[j], out[i]];
  }
  return out.join("");
}
export function generatePassphrase(words = 5, sep = "-") {
  // Short, memorable words; chosen purely at random from WebCrypto
  const W = ("able acid aged also area army away baby back ball band bank base bath bear beat been beer bell belt bird " +
    "blue boat body bone book boot born boss both bowl bulk burn bush busy cake call calm came camp card care case cash " +
    "cast cell chat chip city clay club coal coat code cold come cook cool cope copy core cost crew crop dark data date " +
    "dawn days dead deal dean dear debt deep deny desk dial diet disc disk done door dose down draw drew drop drug dual " +
    "duke dust duty earn ease east easy edge else even ever evil exit face fact fail fair fall farm fast fate fear feed " +
    "feel feet fell felt file fill film find fine fire firm fish five flat flow folk food foot ford form fort four free " +
    "fuel full fund gain game gate gave gear gift girl give glad goal goes gold golf gone good gray grew grey grid grow " +
    "gulf hair half hall hand hang hard harm hate have head hear heat held hell help here hero high hill hire hold hole " +
    "holy home hope host hour huge hung hunt hurt idea inch iron item jack jane jazz join jump june jury keen keep kept " +
    "kick kind king knee knew know lack lady laid lake land lane last late lead left lend lens less life lift like line " +
    "link list live load loan lock long look lord lose loss lost love luck lung made mail main make male mall many mark").split(/\s+/);
  const out = [];
  for (let i = 0; i < words; i++) out.push(W[crypto.getRandomValues(new Uint32Array(1))[0] % W.length]);
  return out.join(sep);
}

// Rough strength estimate (entropy heuristic) 0..4
export function strength(pw) {
  if (!pw) return 0;
  let pool = 0;
  if (/[a-z]/.test(pw)) pool += 26;
  if (/[A-Z]/.test(pw)) pool += 26;
  if (/[0-9]/.test(pw)) pool += 10;
  if (/[^a-zA-Z0-9]/.test(pw)) pool += 30;
  const bits = pw.length * Math.log2(pool || 1);
  return bits < 40 ? 0 : bits < 60 ? 1 : bits < 80 ? 2 : bits < 110 ? 3 : 4;
}
