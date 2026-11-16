# Decoderstand der Implementierung

Der hier beschriebene Stand ersetzt für die neue Anwendung die bloße Bestandsaufnahme des früheren SimpleSDR-Decoders. Es wird kein fremder, ungeprüfter Decoder als fertig integrierter Empfangspfad ausgegeben.

## Gemeinsame Schnittstelle

`krakendf.decoders.DecodedPacket` enthält `payload`, `modulation`, `sample_start`, `sample_end` und `quality`. Die Samplegrenzen beziehen sich auf die an den Decoder übergebene einkanalige IQ-Aufnahme, nicht auf die Verarbeitungszeit. Die übergeordnete Pipeline ergänzt Stream-Epoche, RF-Kanal, Burst-ID und die Abbildung auf den kohärenten Fünfkanalstrom.

`parse_bft_payload` erkennt das vollständige Positionsformat sowie die No-GPS- und LoRa-Testnachrichten der BFT-Firmware. Eine Testnachricht hat eine Identität, aber keine Referenzposition für RMS. Plausibilitätsprüfungen betreffen Koordinatenbereich, Uhrzeit, Textlänge und erlaubte Zeichen. `position_valid` bedeutet eine vollständige plausible Textposition, keine unabhängig bestätigte GPS-Genauigkeit. Positionsalter, HDOP und Datum fehlen im übertragenen Format.

## POCSAG

Die neue unabhängige Implementierung verwendet komplexe FSK-Demodulation, Frequenzoffsetschätzung, Integration über Bitintervalle und eine Suche über beide Polaritäten sowie zwanzig Taktphasen. Unterstützt wird der konkrete BFT-Modus mit 1200 Bit/s und ungefähr ±4,5 kHz Hub. Beliebige geeignete Eingangssampleraten werden rational auf die Demodulationsrate umgesetzt.

Der Parser berücksichtigt Batch-Synchronisation, Adressframes und RIC sowie getrennte Nachrichten. Daten verschiedener Adressen werden nicht zusammengefügt. Die erweiterte BCH(32,21,6)-Prüfung schließt die gerade Gesamtparität ein. Sie korrigiert bis zu zwei Fehler pro Codewort und erkennt alle Dreibitfehler. Ein unkorrektierbares Codewort verwirft die betroffene Nachricht. Mehr als drei Bitfehler können prinzipbedingt nicht alle erkannt werden; POCSAG enthält keinen zusätzlichen Payload-CRC.

Die Protokoll-/Demodulationstests prüfen bekannte standardisierte SYNC-/IDLE-Codewörter, sämtliche Ein- und Zweibitfehler sowie alle Dreibitfehler eines Standardcodeworts, verschiedene RIC-Frames, getrennte Nachrichten und Bursts, beide Polaritäten, Frequenzoffsets, 25/48/300-kHz-Eingangsraten, Rauschen und defekte Codewörter. Die Bitgrenzen werden auf die ursprünglichen Eingangssamples zurückgerechnet. Dies bestätigt Softwareeigenschaften und synthetische Empfangsfälle, keinen bereits vermessenen 433-MHz-Kraken-Empfang.

## LoRa

`krakendf.lora_phy` ist eine neue unabhängige NumPy/SciPy-Implementierung. GNU Radio ist dafür nicht erforderlich. Sie verarbeitet vollständige, kanalisierte Bursts mit explizitem Header und Payload-CRC; nur Pakete mit gültigem Header und CRC gelangen durch `decode_lora` in die Zuordnung zu Rufzeichen und Position. Eine reine Chirperkennung genügt dafür nicht.

Im Unterschied zum früheren eigenen SimpleSDR-Empfänger wurde die vorhandene reale Repository-Aufnahme auf 868,3 MHz erfolgreich decodiert: `DL7CR TESTmessageLoRaTST`, gültiger Header und CRC, SF9/BW125k/CR4/5. Die Aufnahme ist einkanalig und stammt nicht von der Nutzerhardware auf 433 MHz. Der Decoder bleibt deshalb als `experimental` mit `hardware_validated=False` gekennzeichnet. Ein erreichbarer SNR, eine Mindestpaketdauer oder die DF-Genauigkeit werden daraus nicht abgeleitet.

Der PHY behandelt Frequenzversatz, Präambel-/SFD-Suche, Symboldecodierung, Gray-Abbildung, Interleaving, Hamming-Code, Headerprüfung, Whitening und LoRa-Payload-CRC. Der bekannte reale Fixture-Erfolg stellt einen Interoperabilitätscheck dar; zusätzliche synthetische Sendemodelle dienen Regressionstests, nicht einem unabhängigen HF-Nachweis.

Eine korrelative Präambelhilfe ist als `detect_lora_preamble()` verfügbar. Sie verlangt vier aufeinanderfolgende konsistente dechirpte Symbole und gibt einen groben Samplebereich sowie `peak_background_db` zurück. Dieser Wert beschreibt FFT-Konzentration nach Dechirping, kein kalibriertes RF-SNR. Der feste synthetische Test mit −10 dB vor der Verarbeitung prüft Trigger und CRC-Decode unter Rauschen; er ist keine Empfindlichkeitszusage.

LoRa-Regressionen umfassen außerdem 300-kHz-Eingangsraten, positive/negative Frequenzversätze, mehrere Pakete mit getrennten Samplefenstern, alle unterstützten SF-/FEC-Modi einschließlich LDRO, unvollständige Pakete, Rauschen/Dauerträger und absichtlich falschen CRC. Insgesamt bestehen 16 Tests des gemeinsamen Decodermoduls und 11 Tests des LoRa-PHY.

## Verarbeitung außerhalb der Decoder

Die Decoder erhalten einen vollständigen einkanaligen Ausschnitt mit Vorlauf; die Peilung verwendet die zugehörigen kohärenten Ausschnitte aller fünf Antennen. CAL-Frames, verlorene Locks und Lücken dürfen nicht einfach zusammengefügt werden. Nach Diskontinuitäten muss die Pipeline den Empfangszustand zurücksetzen. Für jeden RF-Kanal läuft eine eigene Verarbeitung; die zuletzt gelesene Stationskennung ist keine gültige Paketzuordnung.
