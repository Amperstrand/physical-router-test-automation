// Mint a batch of independent signet tokens from signut.cashu.exchange via the
// admin grant flow. Each token = its own grant + mint, so each is independently
// spendable (campaign phases consume one token per payment).
//
// Output: /tmp/phase-0/tokens.json  — [{amount, v3, proofs}]
import fs from "fs";
const base = "/Users/macbook/src/pecan/web/node_modules/@cashu/cashu-ts";
const { Mint, Wallet } = await import(base + "/lib/cashu-ts.es.js");

const MINT_URL = "https://signut.cashu.exchange";
const KEY = JSON.parse(fs.readFileSync("/Users/macbook/src/cashu-cf/.operator-secrets/admin-keys.json", "utf8")).signut;

// batch: 6 x 4-sat + 6 x 1-sat (Phase A/C/D/E + buffer)
const BATCH = [4,4,4, 1,1,1,1,1];

async function mintOne(amount) {
  const grantRes = await fetch(MINT_URL + "/v1/admin/grants", {
    method: "POST",
    headers: { "Content-Type": "application/json", Authorization: "Bearer " + KEY },
    body: JSON.stringify({ amount, unit: "sat", memo: "tollgate 0.6.0 validation campaign" }),
  });
  const grant = await grantRes.json();
  if (!grantRes.ok) throw new Error("grant failed: " + JSON.stringify(grant).slice(0, 300));
  const quoteId = grant.quote;

  const mint = new Mint(MINT_URL);
  const wallet = new Wallet(mint, { unit: "sat" });
  await wallet.loadMint();
  const proofs = await wallet.mintProofsBolt11(amount, quoteId);
  const total = proofs.reduce((s, p) => s + Number(p.amount), 0);
  if (total !== amount) throw new Error(`minted ${total}, expected ${amount}`);
  const v3 = "cashuA" + Buffer.from(JSON.stringify({ token: [{ mint: MINT_URL, proofs }] })).toString("base64url");
  return { amount, v3, proofs };
}

const out = [];
for (const amt of BATCH) {
  const t = await mintOne(amt);
  out.push(t);
  console.error(`minted ${t.amount} sat (${t.proofs.length} proofs)`);
}
fs.mkdirSync("/tmp/phase-0", { recursive: true });
fs.writeFileSync("/tmp/phase-0/tokens.json", JSON.stringify(out, null, 1));
console.log(`OK ${out.length} tokens -> /tmp/phase-0/tokens.json`);
