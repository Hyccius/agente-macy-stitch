# Agente Instagram di Macy & Stitch 🐾

Pubblica da solo sul profilo Instagram: sceglie foto e video dalla cartella Google Drive "Cani Instagram",
scrive le didascalie con Claude e ogni tanto crea immagini con l'IA (Gemini) partendo dalle foto vere dei cani.

**Quando pubblica:** lunedì, mercoledì, venerdì e domenica verso le 18 (le 19 con l'ora legale).

## Come si usa ogni giorno
- Carica foto e video dei cani nella cartella Drive "Cani Instagram" (anche in sottocartelle).
- Nella sottocartella **riferimenti** tieni 2-3 foto nitide per cane, con il nome del cane nel nome del file
  (es. `macy1.jpg`, `stitch2.jpg`): servono all'IA per disegnarli fedeli.
- Per cambiare carattere dei cani o tono dei post, modifica il file `cani.md` qui su GitHub (icona della matita).

## Avviarlo a mano
Scheda **Actions** → "Agente Instagram Macy e Stitch" → **Run workflow**.
- "Solo prova" spuntato: non pubblica, ma prepara il post. Lo trovi in fondo alla pagina dell'esecuzione,
  sotto **Artifacts → anteprima** (immagine + didascalia), e il testo nel riepilogo.
- "Tipo di post": lascia `auto` oppure forza `foto`, `carosello`, `reel` o `ia`.

## Se qualcosa va storto
GitHub ti manda un'email. Apri l'esecuzione rossa in **Actions**: in fondo al riepilogo c'è la riga "❌ Errore".

## Le chiavi (Settings → Secrets and variables → Actions)
| Nome | Cosa contiene |
|---|---|
| `ANTHROPIC_API_KEY` | chiave Anthropic (Claude) |
| `GEMINI_API_KEY` | chiave Google AI Studio (Gemini) |
| `IG_TOKEN` | token di Instagram |
| `IG_USER_ID` | ID dell'account Instagram |
| `GOOGLE_SA_JSON` | tutto il contenuto del file JSON dell'account di servizio Google |
| `DRIVE_FOLDER_ID` | codice della cartella Drive "Cani Instagram" |

## Note
- Il repository deve essere **pubblico**, perché Instagram scarica le foto da un link pubblico. Le chiavi
  restano comunque segrete. Le foto passano dal repository solo per la pubblicazione e poi vengono rimosse,
  ma restano nella cronologia: sono comunque le stesse foto che finiscono su Instagram.
- `registro.json` tiene traccia di cosa è già stato pubblicato: non cancellarlo.
- Le immagini IA hanno sempre la dicitura "✨ Immagine creata con l'IA…" in didascalia.
