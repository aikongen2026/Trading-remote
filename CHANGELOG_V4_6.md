# 4.6.0-audit-recovery

Kjøpstid, holdetid og posisjonsvern gjenopprettes fra botmerkede Alpaca-ordre etter omstart. En risikokontroll kjører før indikatorer og kontrollerer botens egne aksjeposisjoner, også uten bars og utenfor dagens univers. Ved maksimal holdetid, børsslutt eller bortfalt forventet stopordre brukes eksisterende exit-koordinator; eksisterende salgsintensjon avstemmes før ny ordre. Pause blokkerer nye kjøp, mens beskyttende exits fortsetter. STOP/kill og testmodus sender ingen automatiske exits. Manuelle/ukjente posisjoner overtas ikke av denne kontrollen.

Kjøp krever ferske brokerdata, gyldig tidsstemplet bid/ask, ferske bars og 15 sekunder sammenhengende kvalifisert signal. Dette er en testbar regel, ikke dokumentasjon på økt avkastning. Stoppavstand og størrelse er ikke økt. Posisjoner som kjøpes i samme skannerunde teller umiddelbart mot antallsgrensene.

Rettet manglende runtime_bot_version-funksjon som hindret beslutningslogging. Beslutninger inkluderer nå router, strategi, sektorkontekst, bid/ask, plan, utvalgte innstillinger og ordrekobling. SQLite-forbindelser lukkes eksplisitt. Lagringsfeil vises og blokkerer nye aksjekjøp når beslutningsloggen svikter. Kalibrering teller ENTRY-hendelser, ikke gjentatte BUY-snapshots. 5/15/60-minutters læringsutfall har avgrenset tidsvindu; neste dag kan ikke telle som et én-timesutfall.

AI_ANALYSE.json inkluderer lokale minuttbars, risikoexits med ordrekobling, lagringsstatus, bars-dekning og statistikk per kjøpsrevisjon. Feeden auto blir løst til IEX/SIP før historiske bars hentes. HTTP-status/valgt feed lagres ved feil. Lokal kurslogg brukes som supplement når API-historikk mangler; hull merkes. Kun faktisk ordrekobling kan overstyre exitårsaken, ikke en tilfeldig nærliggende beslutning. MFE/MAE er fortsatt tilnærminger fra minuttbars.

Revisjon og build vises ved Trading Bot øverst, i status og eksport. Lokal risikohistorikk/minuttbars beholdes i 35 dager. Oppdatering går gjennom samme GitHub/Render-mekanisme som tidligere, og nye ordre får v460-tag.

## Drift og begrensninger

Render Free kan stoppe uten trafikk og mister lokale filer ved restart/deploy. Denne revisjonen gjør dette synlig. PAPER kan fortsatt samle data mens prosessen kjører; LIVE aksjekjøp blokkeres på registrert midlertidig Render-lagring. Varig læring krever montert disk (TRADINGBOT_STATE_DIR på diskens sti, for eksempel /var/data/tradingbot) eller separat varig datalagring. Eksisterende render.yaml beskriver disk og compute, men en kodepush endrer ikke nødvendigvis en manuelt opprettet tjenestes abonnement eller miljø. Ingen hostingoppgradering er kjøpt automatisk.

Ordredata kan gjenopprette holdetid når botens gjenværende lots kan matches til posisjonen. Ordrevinduet er 500 nyeste + alle åpne ordre (opptil 500); uavklart eierskap varsles. Ingen kode kan lukke posisjoner mens serveren er stoppet. Ny strategi bør evalueres i PAPER over flere handelsdager med positivt resultat etter kostnader før innsatsen økes.

## Validering

V4.6-regresjonstester dekker tidsgrenser etter omstart, børsslutt, manglende beskyttelse, manuelle posisjoner, FIFO/delvis lukking, pause/kill, reell engine-loop uten bars, signalbekreftelse, feedvalg, logging/kontekst, avgrensede utfall og kalibreringsduplikater. Eksisterende ordresikkerhets-, V4.4-, V4.4.1-, eksport- og V4.5-tester kjøres i tillegg.
