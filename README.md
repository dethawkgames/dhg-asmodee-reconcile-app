# Asmodee Quote Reconciliation

A small webpage where you upload the Asmodee shipping quote PDF you receive by email each Monday. It automatically:

1. Parses the line items (SKU + quantity) out of the PDF
2. Pulls whatever is currently in the **Asmodee Order** tab of the [aggregation sheet](https://docs.google.com/spreadsheets/d/1rsUU7qZJZGhivsofBiFPa7FK6qnHosrxps10NYzLxAE/edit) (this is what you submitted that morning)
3. Compares the two by SKU and quantity
4. Shows the comparison on screen, and writes it to a **Latest Reconciliation** tab in that same sheet (overwritten each time you run it)

## Comparison categories

| Status | Meaning |
|---|---|
| **Match** | Submitted quantity = quoted quantity |
| **More than submitted** | Quoted more than you submitted — likely a preorder/backorder shipping alongside this order |
| **Less than submitted** | Quoted less than you submitted — possible partial shipment |
| **Missing from quote entirely** | You submitted it, but it's not on the quote at all — sales rep may have removed it or it's unavailable. Needs a look. |
| **In quote but not submitted** | It's on the quote but wasn't in your Asmodee Order tab — unexpected addition, worth checking |

## Important: do this same-day

The aggregation tool overwrites the Asmodee Order tab every Monday morning. This reconciliation tool always compares against **whatever is currently in that tab** — there's no concept of "which week." Run this the same day you get the quote, before the next Monday's aggregation run replaces the data.

## PDF parsing notes

Asmodee's quote PDFs sometimes wrap long SKUs onto a second line (e.g. `UGCSV0101` / `EN` on separate lines). The parser uses word x-position on the page to detect this and reassemble the full SKU — it's not just reading line by line. If Asmodee changes their PDF template/layout in the future, this parsing logic may need adjustment (the column x-positions are hardcoded based on the sample quote used to build this).

## Deployment

This project uses Vercel's **Python runtime** (currently in Beta) for the PDF-parsing endpoint, since it reuses the same `pdfplumber` logic already validated against a real sample quote — rather than rewriting it in JavaScript.

### 1. Push to GitHub, import to Vercel

```bash
git init
git add .
git commit -m "Initial Asmodee quote reconciliation tool"
git remote add origin https://github.com/dethawkgames/dhg-asmodee-reconcile.git
git push -u origin main
```

Import the repo at vercel.com/new as usual.

### 2. Environment variables

| Variable | Value |
|---|---|
| `GOOGLE_SA_EMAIL` | `dhg-sheets-bot@dhg-automation.iam.gserviceaccount.com` |
| `GOOGLE_SA_PRIVATE_KEY` | Full private key from the service account JSON, including BEGIN/END lines |

### 3. Deploy, then test

Open the deployed URL, upload a sample quote PDF, and confirm the comparison table renders and the Latest Reconciliation tab gets written.

## Known issue: service account keys have failed unexpectedly mid-session

While building this, we hit "Invalid JWT Signature" errors from Google's OAuth token endpoint **multiple times**, each time resolved only by generating a brand-new key in Cloud Console. This happened even though:
- The key content was verified byte-for-byte correct
- The JWT was self-verified as cryptographically valid against the key's own derived public key
- Clocks were in sync
- No one had touched Cloud Console in between

This is unusual and not fully explained. If you hit `invalid_grant` / `Invalid JWT Signature` again:
1. Don't assume it's a code bug — the JWT construction has been validated correct multiple times.
2. Generate a fresh key in Cloud Console (IAM & Admin → Service Accounts → `dhg-sheets-bot` → Keys), and update the `GOOGLE_SA_PRIVATE_KEY` environment variable wherever this tool (or any of the other DHG tools using this same service account) is deployed.
3. Consider checking with Google Cloud support or your account's audit logs if this keeps recurring — it may point to an org policy (like a key-rotation/expiry policy) silently revoking keys after a short window, which would explain why each fresh key works immediately but stops working later.

## Mark Supplier Shipped (added later)

Three buttons on the same page let you record when a supplier confirms shipment:

- **Asmodee Shipped** — checks the *Latest Reconciliation* tab (the most recent PDF comparison run). An order only gets marked complete for Asmodee if every one of its Asmodee SKUs has a status of `Match` or the preorder/backorder overage case. If any SKU for that order is short, missing, or unexpected, the order stays pending for Asmodee.
- **Universal Dist Shipped** / **ACDD Shipped** — no itemized confirmation exists for these suppliers (just a credit card charge), so clicking marks every order currently needing that supplier as shipped, unconditionally. This is a deliberate best-guess to keep customers informed; it gets corrected for real once the physical packing slip arrives and you do your real next check.

### How completion works

A new **Shipment Tracking** tab (built automatically each Monday by the Supplier Order Aggregation tool) holds one row per order: `Order # | Suppliers Needed | Suppliers Shipped So Far | Status`. Each button click adds its supplier to "Suppliers Shipped So Far" for every eligible order. Once every supplier an order needs has been checked off — which might happen across multiple button clicks, possibly days apart — the order's status flips to `Complete` and it gets tagged `dhg-shipped-from-supplier` in Shopify automatically.

This means an order with items from two different suppliers needs both buttons clicked (in either order, on either day) before the customer-facing tag is applied.

### Important: this only works for the CURRENT week

The Shipment Tracking tab gets rebuilt fresh every Monday by the aggregation tool. If a supplier's shipment isn't marked before the next Monday's run, that week's tracking data is gone and the button will act on the new week's orders instead. Mark shipments before each Monday's aggregation run.
