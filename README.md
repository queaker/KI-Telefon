![GitHub Logo](http://www.heise.de/make/icons/make_logo.png)

Maker Media GmbH

***

# KI-Telefon

**In diesem Projekt habe ich einem alten „Telekom 611-2“-Tischtelefon neues Leben eingehaucht. Abheben, wählen, sprechen – genau wie früher, nur dass am anderen Ende kein Mensch, sondern eine KI den Hörer abnimmt. Ein Raspberry Pi und die API-Schnittstelle von OpenAI machen Echtzeitgespräche möglich.**

![Aufmacherbild aus dem Heft](./doc/kiTelefon_github.jpg)

Die benötigten Dateien für das Projekt liegen in diesem GitHub-Repository.

Der vollständige Artikel zum Projekt steht in der **[Make-Ausgabe 2/26](https://www.heise.de/select/make/2026/2)**.

## Hinweis zu diesem Fork

Dieser Fork baut auf dem ursprünglichen KI-Telefon-Projekt auf und ergänzt die Firmware um robustere Bedienlogik, einen konfigurierbaren Automatikmodus, bessere Wählscheiben-Diagnose und stabilere Audioverarbeitung.

Der Grundgedanke bleibt unverändert: Man hebt den Hörer ab, wählt mit der Wählscheibe eine Nummer und spricht anschließend mit einer KI-Rolle. Die Änderungen betreffen vor allem den Programmablauf in `main.py` und die Realtime-Anbindung in `openai_ws.py`.

## Bedienung

Nach dem Start klingelt das Telefon einmal kurz. Dieses kurze Klingeln ist nur ein Funktionstest und startet noch kein Gespräch.

Danach befindet sich das Telefon im Ruhezustand:

- Hörer abheben: Freizeichen startet und das Telefon wartet auf eine Nummer.
- `1` bis `9` wählen: Eine KI-Rolle wird ausgewählt und angerufen.
- `0` wählen: Automatische eingehende KI-Anrufe werden ein- oder ausgeschaltet.
- Hörer auflegen: Wahl oder Gespräch wird abgebrochen beziehungsweise beendet.

Wenn der Automatikmodus aktiv ist, klingelt das Telefon nach Ablauf der Wartezeit von selbst. Wird dann abgehoben, verbindet die Firmware mit einer zufällig ausgewählten KI-Rolle.

## Änderungen gegenüber dem ursprünglichen Programmablauf

### Automatikmodus statt Shutdown auf `0`

In der ursprünglichen Firmware löste die Ziffer `0` einen Shutdown des Raspberry Pi aus. In diesem Fork schaltet `0` stattdessen den Automatikmodus um.

Die Automatik ist nach dem Programmstart immer deaktiviert und wird nicht dauerhaft gespeichert. Nach einem Neustart muss sie erneut mit `0` aktiviert werden.

Zur Bestätigung wird ein kurzer Signalton abgespielt:

- aufsteigende Tonfolge: Automatik aktiviert
- absteigende Tonfolge: Automatik deaktiviert

### Definierter Startzustand

Beim Start prüft die Firmware, ob der Hörer bereits abgehoben ist. Falls ja, wartet das Programm zunächst darauf, dass der Hörer aufgelegt wird. Erst danach beginnt die eigentliche Ablaufsteuerung.

Dadurch startet die State-Machine nicht versehentlich mitten in einem Wahl- oder Gesprächszustand.

### Auflegen während Wahl und Freiton

Die Firmware reagiert nun auch dann auf das Auflegen, wenn sie gerade auf Wählscheibenimpulse oder den Freiton wartet.

Das verhindert, dass das Programm im Wahlmodus hängen bleibt, wenn der Hörer abgenommen und ohne vollständige Wahl wieder aufgelegt wird.

### Verbesserte Wählscheiben-Auswertung

Die Wählscheibe wird über GPIO-Impulse ausgewertet. Um Kontaktprellen und Störimpulse besser zu erkennen, wurde die Diagnoseausgabe erweitert.

Die Firmware protokolliert beim Wählen unter anderem:

- jeden Rohimpuls,
- den zeitlichen Abstand zum vorherigen Rohimpuls,
- den zeitlichen Abstand zum vorherigen gezählten Impuls,
- ob der Impuls gezählt oder verworfen wurde,
- eine Zusammenfassung nach Abschluss der Wahl.

Der Softwarefilter für den Mindestabstand zwischen zwei gezählten Impulsen wurde auf 80 ms gesetzt:

```python
MIN_PULSE_SEPARATION = 0.08
```

Dieser Wert entstand aus Messungen an der verwendeten Wählscheibe. Echte Impulse lagen dort typischerweise bei rund 95 bis 100 ms Abstand, während falsche Impulse kürzer waren.

### Keine blockierenden Audio-Aufrufe im GPIO-Callback

Audio-Operationen wie das Stoppen des Freizeichens werden nicht mehr direkt im GPIO-Callback ausgeführt. Der Callback setzt nur noch ein Flag; die eigentliche Audio-Operation passiert anschließend im normalen Programmfluss.

Das ist robuster, weil Audiofunktionen je nach Sound-Backend blockieren können und nicht aus einem GPIO-Ereignis heraus ausgeführt werden sollten.

### Stabilere Audioausgabe

Die KI-Audioausgabe verwendet einen kleinen Jitter-Puffer. Die Wiedergabe beginnt erst, wenn ausreichend Audiodaten vorhanden sind. Läuft der Puffer leer, wird kurz Stille ausgegeben und danach erneut auf genügend Daten gewartet.

Zusätzlich wird der Audiopuffer mit einem Lock geschützt, damit WebSocket-Empfang und Lautsprecher-Callback nicht gleichzeitig unkoordiniert darauf zugreifen.

### Aktualisierte Realtime-Anbindung

Die OpenAI-Realtime-Anbindung wurde klarer strukturiert. Dazu gehören:

- eine explizite Realtime-State-Machine,
- sauberere Trennung von Verbindungsaufbau, Session-Update und Audio-Streaming,
- robustere Fehlerausgaben,
- aktualisierte Session-Konfiguration für die neuere Realtime-API-Struktur.

## Wichtige Dateien

- `main.py`: Hauptprogramm, Bedienlogik, Wählscheibe, Automatikmodus, Audioausgabe.
- `bell.py`: Ansteuerung der Telefonklingel.
- `handset.py`: Erkennung, ob der Hörer abgehoben oder aufgelegt ist.
- `openai_ws.py`: WebSocket-Verbindung zur OpenAI-Realtime-API.
- `roles.py`: Definition der auswählbaren KI-Rollen.
- `gespraechspartner.py`: optionale Zusatzinformationen zu bekannten Gesprächspartnern.

## Fehlersuche

### Wählscheibe zählt falsch

Wenn eine Ziffer zu hoch oder zu niedrig erkannt wird, sollte zuerst die Diagnoseausgabe der Wählscheibe betrachtet werden. Interessant sind vor allem die Abstände `dt_raw` und `dt_since_counted`.

Typische Hinweise:

- sehr kleine Abstände im Bereich weniger Millisekunden: Kontaktprellen,
- Abstände deutlich unterhalb echter Impulse: mögliche Störimpulse,
- echte Impulse sollten bei einer mechanischen Wählscheibe relativ regelmäßig auftreten.

Der Filterwert `MIN_PULSE_SEPARATION` kann bei anderer Hardware angepasst werden.

### Programm hängt nach erstem Wählscheibenimpuls

Falls das Programm nach dem ersten Impuls stehen bleibt, sollte geprüft werden, ob im GPIO-Callback noch blockierende Operationen ausgeführt werden. Insbesondere Audio-Aufrufe wie `stop()`, `close()` oder längere Logik sollten dort vermieden werden.

### Gespräch endet nicht beim Auflegen

Die Funktion `monitor_handset()` überwacht während eines Gesprächs den Hörerzustand. Wenn das Auflegen nicht erkannt wird, sollten zuerst GPIO-Pin, Pull-up/Pull-down-Beschaltung und die Logik in `handset.py` geprüft werden.

## Entwicklungsnotizen

Die Firmware ist bewusst einfach gehalten und eignet sich gut zum Experimentieren. Viele Parameter sind direkt im Code konfigurierbar, zum Beispiel:

- Wartezeit bis zum nächsten automatischen Anruf,
- Anzahl der Klingelversuche,
- Mindestabstand zwischen Wählscheibenimpulsen,
- Tonfolgen für den Automatikmodus,
- KI-Rollen und Stimmen.

Pull Requests sollten möglichst kleine, nachvollziehbare Änderungen enthalten und Hardware-spezifische Annahmen im Code kommentieren.
