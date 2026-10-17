# Keyboard-only walkthrough

Recorded by `tools/capture.py` with real `Tab` / `Enter` / `ArrowDown` / `Escape` key events over CDP (no synthetic clicks, no pointer).

## 1. Tab from the address bar to the Incidents destination — PASS

```json
{
  "focusOrder": [
    {
      "tag": "BUTTON",
      "cls": "proto-picker-item",
      "label": "Console",
      "role": null
    },
    {
      "tag": "BUTTON",
      "cls": "proto-picker-item",
      "label": "Briefing",
      "role": null
    },
    {
      "tag": "BUTTON",
      "cls": "proto-picker-item",
      "label": "Instrument",
      "role": null
    },
    {
      "tag": "BUTTON",
      "cls": "proto-picker-item",
      "label": "Overview",
      "role": null
    },
    {
      "tag": "BUTTON",
      "cls": "proto-picker-item proto-picker-replay",
      "label": "Replay animation (R)",
      "role": null
    },
    {
      "tag": "BUTTON",
      "cls": "hig-nav-item hig-on",
      "label": "Fleet",
      "role": null
    },
    {
      "tag": "BUTTON",
      "cls": "hig-nav-item",
      "label": "Incidents",
      "role": null
    }
  ],
  "incidentsFocused": true,
  "tabs": 7
}
```

_every stop is a control the operator can reach with Tab alone_

## 2. Enter on the Incidents destination — PASS

```json
{
  "dest": "incidents",
  "title": "Incidents",
  "focus": {
    "tag": "BUTTON",
    "cls": "hig-nav-item hig-on",
    "label": "Incidents",
    "role": null
  }
}
```

_destination reached without a pointer_

## 3. Arrow keys in the incident list — PASS

```json
{
  "focusedListRow": true,
  "tabsToList": 9,
  "first": "01H4KZ9Q2R3T5V7W9X1Y3Z5A7B9C000020",
  "down1": "01H4KZ9Q2R3T5V7W9X1Y3Z5A7B9C000050",
  "down2": "01H4KZ9Q2R3T5V7W9X1Y3Z5A7B9C000080",
  "up1": "01H4KZ9Q2R3T5V7W9X1Y3Z5A7B9C000050",
  "detailBefore": "ai-generated incident\nincident_generated: \"model\"\nAI model\nAsk AI\nDisk write latency on /dev/sda\n\nwrite_latency_ms on /dev/sda reached 120 ms against a 50 ms th",
  "detailAfterDown1": "",
  "detailAfterUp1": ""
}
```

_selection and the detail pane move together_

## 4. Enter on Operator log, then Escape — PASS

```json
{
  "focusedLogButton": true,
  "opened": true,
  "closed": true,
  "focusReturned": true,
  "focusAfter": {
    "tag": "BUTTON",
    "cls": "hig-btn",
    "label": "Operator log",
    "role": null
  }
}
```

_the sheet is a native dialog: Escape dismisses it and focus returns to the control that opened it_

## 5. Enter on the Settings destination — PASS

```json
{
  "dest": "settings",
  "title": "Settings",
  "focus": {
    "tag": "BUTTON",
    "cls": "hig-seg-item hig-on",
    "label": "System",
    "role": null
  }
}
```

_three destinations, three keyboard stops_
