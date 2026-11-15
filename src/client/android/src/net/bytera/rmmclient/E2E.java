// E2E.java - end-to-end encrypted API calls to the RMM backend (counterpart: backend app/security.py)
// ------------------------------------------------------------------------------------------------
// Every API call of the app travels inside an encrypted envelope (POST /api/e2e):
// P-256 ECDH (fresh key per request) + HKDF-SHA256 + AES-256-GCM. Reverse proxies and
// Cloudflare only see ciphertext - also the password at login and the session token.
// Pure Java (no Android classes), works from Android 6 (API 23).
package net.bytera.rmmclient;

import java.math.BigInteger;
import java.nio.ByteBuffer;
import java.security.KeyFactory;
import java.security.KeyPair;
import java.security.KeyPairGenerator;
import java.security.MessageDigest;
import java.security.PublicKey;
import java.security.SecureRandom;
import java.security.interfaces.ECPublicKey;
import java.security.spec.ECGenParameterSpec;
import java.security.spec.X509EncodedKeySpec;

import javax.crypto.Cipher;
import javax.crypto.KeyAgreement;
import javax.crypto.Mac;
import javax.crypto.spec.GCMParameterSpec;
import javax.crypto.spec.SecretKeySpec;

public final class E2E {
    // DER prefix of a P-256 SubjectPublicKeyInfo; followed by the 65-byte uncompressed point
    private static final byte[] P256_SPKI = hex("3059301306072a8648ce3d020106082a8648ce3d030107034200");
    private static final byte[] INFO = "bytera-e2e-v1|".getBytes();

    private final byte[] kid = new byte[8];
    private final byte[] epk;
    private final byte[] salt = new byte[16];
    private final byte[] kC2S;
    private final byte[] kS2C;

    /** serverKey: the backend's public key (65 bytes, uncompressed P-256 point). */
    public E2E(byte[] serverKey) throws Exception {
        byte[] full = MessageDigest.getInstance("SHA-256").digest(serverKey);
        System.arraycopy(full, 0, kid, 0, 8);
        String kidHex = toHex(kid);

        KeyPairGenerator g = KeyPairGenerator.getInstance("EC");
        g.initialize(new ECGenParameterSpec("secp256r1"), new SecureRandom());
        KeyPair eph = g.generateKeyPair();
        epk = rawPoint((ECPublicKey) eph.getPublic());
        new SecureRandom().nextBytes(salt);

        byte[] spki = new byte[P256_SPKI.length + serverKey.length];
        System.arraycopy(P256_SPKI, 0, spki, 0, P256_SPKI.length);
        System.arraycopy(serverKey, 0, spki, P256_SPKI.length, serverKey.length);
        PublicKey srv = KeyFactory.getInstance("EC").generatePublic(new X509EncodedKeySpec(spki));
        KeyAgreement ka = KeyAgreement.getInstance("ECDH");
        ka.init(eph.getPrivate());
        ka.doPhase(srv, true);
        byte[] shared = ka.generateSecret();

        byte[] info = concat(INFO, kidHex.getBytes());
        byte[] okm = hkdf(salt, shared, info, 64);
        kC2S = new byte[32];
        kS2C = new byte[32];
        System.arraycopy(okm, 0, kC2S, 0, 32);
        System.arraycopy(okm, 32, kS2C, 0, 32);
    }

    /** Body for POST /api/e2e: "BXE1" kid epk salt seal(c2s, request json). */
    public byte[] request(byte[] requestJson) throws Exception {
        byte[] nonce = nonce("c2s\0".getBytes(), 0);
        Cipher c = Cipher.getInstance("AES/GCM/NoPadding");
        c.init(Cipher.ENCRYPT_MODE, new SecretKeySpec(kC2S, "AES"), new GCMParameterSpec(128, nonce));
        c.updateAAD("req".getBytes());
        byte[] ct = c.doFinal(requestJson);
        return concat("BXE1".getBytes(), kid, epk, salt, nonce, ct);
    }

    /** Decrypted response json (UTF-8 bytes). */
    public byte[] response(byte[] blob) throws Exception {
        if (blob.length < 28 || blob[0] != 's' || blob[1] != '2' || blob[2] != 'c' || blob[3] != 0)
            throw new Exception("bad response");
        byte[] nonce = new byte[12];
        System.arraycopy(blob, 0, nonce, 0, 12);
        Cipher c = Cipher.getInstance("AES/GCM/NoPadding");
        c.init(Cipher.DECRYPT_MODE, new SecretKeySpec(kS2C, "AES"), new GCMParameterSpec(128, nonce));
        c.updateAAD("resp".getBytes());
        return c.doFinal(blob, 12, blob.length - 12);
    }

    // ---------------------------------------------------------------- helpers
    public static String sha256Hex(byte[] data) throws Exception {
        return toHex(MessageDigest.getInstance("SHA-256").digest(data));
    }

    static byte[] hkdf(byte[] salt, byte[] ikm, byte[] info, int len) throws Exception {
        Mac mac = Mac.getInstance("HmacSHA256");
        mac.init(new SecretKeySpec(salt, "HmacSHA256"));
        byte[] prk = mac.doFinal(ikm);
        mac.init(new SecretKeySpec(prk, "HmacSHA256"));
        ByteBuffer out = ByteBuffer.allocate(len);
        byte[] t = new byte[0];
        for (int i = 1; out.position() < len; i++) {
            mac.update(t);
            mac.update(info);
            mac.update((byte) i);
            t = mac.doFinal();
            out.put(t, 0, Math.min(t.length, len - out.position()));
        }
        return out.array();
    }

    static byte[] rawPoint(ECPublicKey k) {
        byte[] out = new byte[65];
        out[0] = 4;
        put32(k.getW().getAffineX(), out, 1);
        put32(k.getW().getAffineY(), out, 33);
        return out;
    }

    private static void put32(BigInteger v, byte[] out, int off) {
        byte[] b = v.toByteArray();
        int start = Math.max(0, b.length - 32);
        int n = b.length - start;
        System.arraycopy(b, start, out, off + 32 - n, n);
    }

    private static byte[] nonce(byte[] prefix, long counter) {
        return ByteBuffer.allocate(12).put(prefix).putLong(counter).array();
    }

    static byte[] concat(byte[]... parts) {
        int n = 0;
        for (byte[] p : parts) n += p.length;
        ByteBuffer b = ByteBuffer.allocate(n);
        for (byte[] p : parts) b.put(p);
        return b.array();
    }

    static String toHex(byte[] b) {
        StringBuilder s = new StringBuilder();
        for (byte x : b) s.append(String.format("%02x", x & 0xff));
        return s.toString();
    }

    static byte[] hex(String s) {
        byte[] out = new byte[s.length() / 2];
        for (int i = 0; i < out.length; i++) out[i] = (byte) Integer.parseInt(s.substring(2 * i, 2 * i + 2), 16);
        return out;
    }
}
