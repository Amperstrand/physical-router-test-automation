import fs from "fs";
const base = "/Users/macbook/src/pecan/web/node_modules/@cashu/cashu-ts";
const { Mint, Wallet } = await import(base + "/lib/cashu-ts.es.js");
const MINT_URL = "https://testnut.cashu.exchange";
const KEY = JSON.parse(fs.readFileSync("/Users/macbook/src/cashu-cf/.operator-secrets/admin-keys.json", "utf8")).testnut;
const BATCH = Array(150).fill(4);
async function mintOne(amount) {
  const grantRes = await fetch(MINT_URL + "/v1/admin/grants", {
    method: "POST",
    headers: { "Content-Type": "application/json", Authorization: "Bearer " + KEY },
    body: JSON.stringify({ amount, unit: "sat", memo: "tollgate 24h soak" }),
  });
  const grant = await grantRes.json();
  if (!grantRes.ok) throw new Error("grant failed: " + JSON.stringify(grant).slice(0, 200));
  const mint = new Mint(MINT_URL);
  const wallet = new Wallet(mint, { unit: "sat" });
  await wallet.loadMint();
  const proofs = await wallet.mintProofsBolt11(amount, grant.quote);
  const total = proofs.reduce((s, p) => s + Number(p.amount), 0);
  if (total !== amount) throw new Error(`minted ${total}`);
  return { amount, v3: "cashuA" + Buffer.from(JSON.stringify({ token: [{ mint: MINT_URL, proofs }] })).toString("base64url"), proofs: proofs.map(p => ({ ...p, amount: Number(p.amount) })) };
}
const out = [];
for (const amt of BATCH) { out.push(await mintOne(amt)); process.stderr.write("."); }
fs.writeFileSync("/tmp/phase-0/testnut-tokens.json", JSON.stringify(out));
console.log(`OK ${out.length} testnut tokens`);
