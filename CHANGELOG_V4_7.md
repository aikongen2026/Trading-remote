# 4.7.0-broker-history

Ny Alpaca trade_updates-forbindelse lagrer fills, delvise fills, avslag og kanselleringer uten API-nøkler. Binære PAPER-meldinger støttes, duplikater fjernes, og strømmen ber om ny REST-avstemming. REST er fortsatt autoritet for ordre og posisjoner. Hvert femte minutt forsøkes full tilbakehenting av 35 dagers ordre/aktiviteter med paginering. Hull/avkorting markeres som feil. Ingen eldre meglerhistorikk slettes av denne oppdateringen.

AI_ANALYSE.json inkluderer hendelsesarkiv, stream-/synkstatus, registrerte USD-gebyrkontantstrømmer og tydelige databegrensninger. Gebyrets bokføringsdato kan avvike fra handelsdato. Ikke-USD-gebyrer blir ikke vilkårlig konvertert. Ordre som mangler i vinduet hentes fra tilknyttede aktiviteter. Beslutninger og shadow-rader avgrenses av samme sluttid, også ved ulike tidssoner.

15-sekunders kjøpsbekreftelse tåler nå skannerunder over fem sekunder. Ugyldig signal eller langt opphold nullstiller bekreftelsen. Hele aksjer avrundes etter reduserte risiko-/posisjonsgrenser; én aksje tillates kun når begge grenser tillater den. Innstillingen crypto_min_net_edge_bps brukes i planen. Manglende/ugyldig/gammel kryptokvotering blokkerer nye kjøp. Kjøpsintensjon lagres før innsending; nettverksfeil tillater ikke nytt kjøp før REST-avstemming. Restart gjenbruker kjent kjøpstid for kryptoholdetid.

## Varig lagring

Montert /var/data oppdages automatisk hvis TRADINGBOT_STATE_DIR ikke er satt. En vanlig skrivbar mappe på Render regnes fortsatt som midlertidig. Valgfri TRADINGBOT_DATABASE_URL (PostgreSQL) gir kontrollert SQLite-backup av learning_v3.sqlite, runtime_audit.sqlite og broker_history.sqlite hvert 300. sekund. PostgreSQL-brukeren må kunne opprette og oppdatere tradingbot_state_backup. Sett eventuelt en stabil, unik TRADINGBOT_BACKUP_NAMESPACE; PAPER/LIVE har egne navnerom. Bruk en varig database med TLS iht. leverandørens tilkoblingsstreng.

Ved oppstart gjenopprettes manglende evidensdatabaser etter hash-/integritetskontroll. Eksisterende lokale databaser overskrives aldri. START/STOP, kjøps-/salgsintensjoner, passord og nøkler blir ikke lagret i databasebackupen. Mislykket gjenoppretting blokkerer nye kjøp og krever restart etter retting. Backup kan miste siste fem minutters data, eller mer ved feil; status viser siste vellykkede tidspunkt. LIVE krever fortsatt montert varig disk for utførelsestilstand.

Ingen database, betalt disk eller hostingoppgradering er opprettet automatisk. Render Free kan fortsatt sovne og miste lokalfiler. Uten konfigurert varig lagring forblir lokal læring og hendelsesarkiv midlertidige; brokerhistorikk forsøkes hentet på nytt. Analyseeksport beholder én AI_ANALYSE.json. Sikker oppstart kan kreve START etter deploy; sjekk PAPER og posisjoner først.

## Validering og begrensninger

v47_test.py: 12 tester for binære meldinger, deduplisering, autorisasjonsavslag, tilbakehenting, feil ved avkortet paginering, tidssonegrenser, treg bekreftelse, ukjent kjøp etter restart, avstemmingsfrist, risikoavrunding, kryptoterskel og ekte SQLite checkpoint/gjenoppretting via simulert PostgreSQL. Eksisterende safety/V4.4/V4.4.1/V4.4.2/V4.5/V4.6-suiter kjøres også. Eksterne ordre sendes ikke av testene. Reell PostgreSQL-tilkobling er ikke verifisert uten brukerkonfigurert database.

Dette retter data- og utførelsesfeil; økt avkastning er ikke dokumentert. Siste dags få handler gir ikke grunnlag for sikker strategioptimalisering. Modellens P(up) er ikke validert som sannsynlighet for take-profit før stop-loss. EV er fortsatt en modell, og kryptovern er fortsatt avhengig av tilgjengelige markedsdata. Verifiser flere PAPER-dager etter kostnader før innsatsen økes.

Alpaca-dokumentasjon: https://docs.alpaca.markets/us/docs/websocket-streaming
