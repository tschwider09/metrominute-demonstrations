(function () {
  const bootstrapEl = document.getElementById("bootstrap-data");
  const bootstrap = bootstrapEl ? JSON.parse(bootstrapEl.textContent || "{}") : {};

  const tabButtons = Array.from(document.querySelectorAll(".tab-btn[data-tab]"));
  const panels = Array.from(document.querySelectorAll(".panel[data-panel]"));
  const stationOptionsEl = document.getElementById("station-options");
  const fromInputEl = document.getElementById("from-input");
  const toInputEl = document.getElementById("to-input");
  const planFormEl = document.getElementById("plan-form");
  const plannerMessageEl = document.getElementById("planner-message");
  const summaryGridEl = document.getElementById("summary-grid");
  const lineChipsEl = document.getElementById("line-chips");
  const legsListEl = document.getElementById("legs-list");
  const stationSearchEl = document.getElementById("station-search");
  const stationsListEl = document.getElementById("stations-list");
  const runtimeJsonEl = document.getElementById("runtime-json");
  const refreshRuntimeEl = document.getElementById("refresh-runtime");

  const stationRows = Array.isArray(bootstrap.stations) ? bootstrap.stations : [];
  const stationById = new Map();
  const stationByLabel = new Map();
  stationRows.forEach(function (row) {
    const stationId = String((row && row.station_id) || "").toUpperCase();
    if (stationId) {
      stationById.set(stationId, row);
    }
    const label = String((row && row.label) || "").trim().toLowerCase();
    if (label) {
      stationByLabel.set(label, stationId);
    }
  });

  const routeColors = {
    "1": "#db2f2f",
    "2": "#db2f2f",
    "3": "#db2f2f",
    "4": "#10a54a",
    "5": "#10a54a",
    "6": "#10a54a",
    "7": "#b64cc2",
    A: "#1f5ea8",
    B: "#ff6a2b",
    C: "#1f5ea8",
    D: "#ff6a2b",
    E: "#1f5ea8",
    F: "#ff6a2b",
    G: "#5bb23f",
    J: "#8b5e2f",
    L: "#8a8d91",
    M: "#ff6a2b",
    N: "#f1bf10",
    Q: "#f1bf10",
    R: "#f1bf10",
    W: "#f1bf10",
    S: "#6b6f75",
  };

  function escapeHtml(value) {
    return String(value || "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#039;");
  }

  function setActiveTab(tabName) {
    const normalized = String(tabName || "plan").toLowerCase();
    tabButtons.forEach(function (button) {
      button.classList.toggle("is-active", button.dataset.tab === normalized);
    });
    panels.forEach(function (panel) {
      panel.classList.toggle("is-active", panel.dataset.panel === normalized);
    });
  }

  function setPlannerMessage(text, isError) {
    if (!plannerMessageEl) {
      return;
    }
    const cleaned = String(text || "").trim();
    plannerMessageEl.textContent = cleaned;
    plannerMessageEl.hidden = !cleaned;
    plannerMessageEl.classList.toggle("is-error", Boolean(isError && cleaned));
  }

  function renderStationOptions(rows) {
    if (!stationOptionsEl) {
      return;
    }
    stationOptionsEl.innerHTML = "";
    rows.forEach(function (row) {
      const option = document.createElement("option");
      option.value = String(row.label || row.station_id || "");
      stationOptionsEl.appendChild(option);
    });
  }

  function resolveStationId(rawValue) {
    const value = String(rawValue || "").trim();
    if (!value) {
      return "";
    }
    const upper = value.toUpperCase();
    if (stationById.has(upper)) {
      return upper;
    }
    const fromLabel = stationByLabel.get(value.toLowerCase());
    if (fromLabel) {
      return fromLabel;
    }
    const match = value.match(/\(([^)]+)\)$/);
    if (match && stationById.has(String(match[1] || "").toUpperCase())) {
      return String(match[1] || "").toUpperCase();
    }
    return "";
  }

  function collectWeightParams() {
    const getValue = function (id) {
      const el = document.getElementById(id);
      if (!el) {
        return "";
      }
      return String(el.value || "").trim();
    };
    const params = new URLSearchParams();
    const mappings = [
      ["ride_multiplier", getValue("ride-multiplier")],
      ["line_switch_penalty", getValue("line-switch-penalty")],
      ["station_transfer_penalty", getValue("station-transfer-penalty")],
      ["direction_switch_penalty", getValue("direction-switch-penalty")],
      ["transfer_time_multiplier", getValue("transfer-time-multiplier")],
      ["default_wait_minutes", getValue("default-wait-minutes")],
      ["arrival_limit", getValue("arrival-limit")],
    ];
    mappings.forEach(function (entry) {
      if (entry[1] !== "") {
        params.set(entry[0], entry[1]);
      }
    });
    const dynamicWaitEl = document.getElementById("dynamic-wait-enabled");
    if (dynamicWaitEl) {
      params.set("dynamic_wait_enabled", dynamicWaitEl.checked ? "1" : "0");
    }
    return params;
  }

  function renderSummary(payload) {
    const summary = (payload && payload.summary) || {};
    const weightedMinutes = summary.weighted_minutes;
    const transferCount = summary.transfer_count;
    const stationCount = summary.station_count;
    const distanceMiles = payload.direct_distance_miles;

    if (summaryGridEl) {
      summaryGridEl.innerHTML = [
        { label: "ETA", value: weightedMinutes != null ? String(weightedMinutes) + " min" : "--" },
        { label: "Transfers", value: transferCount != null ? String(transferCount) : "--" },
        { label: "Stations", value: stationCount != null ? String(stationCount) : "--" },
        { label: "Distance (mi)", value: distanceMiles != null ? String(distanceMiles) : "--" },
      ]
        .map(function (item) {
          return (
            '<div class="summary-item"><span>' +
            escapeHtml(item.label) +
            "</span><strong>" +
            escapeHtml(item.value) +
            "</strong></div>"
          );
        })
        .join("");
    }

    if (lineChipsEl) {
      const lines = Array.isArray(payload.suggested_lines) ? payload.suggested_lines : [];
      if (!lines.length) {
        lineChipsEl.innerHTML = "";
      } else {
        lineChipsEl.innerHTML = lines
          .map(function (line) {
            const normalized = String(line || "").toUpperCase();
            const color = routeColors[normalized] || "#2c7fb8";
            return (
              '<span class="line-chip" style="--line-color:' +
              escapeHtml(color) +
              '">' +
              escapeHtml(normalized) +
              "</span>"
            );
          })
          .join("");
      }
    }

    const legs = (((payload || {}).path || {}).legs) || [];
    if (!legsListEl) {
      return;
    }
    if (!Array.isArray(legs) || !legs.length) {
      legsListEl.innerHTML = '<li class="leg-empty">No leg details were returned for this route.</li>';
      return;
    }
    legsListEl.innerHTML = legs
      .map(function (leg) {
        const kind = String(leg.kind || "").toLowerCase();
        const minutes = leg.minutes != null ? Number(leg.minutes).toFixed(2) + " min" : "--";
        if (kind === "ride") {
          const line = String(leg.line || "").toUpperCase();
          const direction = String(leg.direction || "").toUpperCase();
          const fromName = String(leg.from_name || leg.from_station || "");
          const toName = String(leg.to_name || leg.to_station || "");
          return (
            '<li class="leg-item ride-leg">' +
            '<div class="leg-head"><strong>' +
            escapeHtml(line + " ride " + direction) +
            "</strong><span>" +
            escapeHtml(minutes) +
            "</span></div>" +
            '<p class="leg-meta">' +
            escapeHtml(fromName + " -> " + toName) +
            "</p></li>"
          );
        }
        const fromName = String(leg.from_name || leg.from_station || "");
        const toName = String(leg.to_name || leg.to_station || "");
        return (
          '<li class="leg-item transfer-leg">' +
          '<div class="leg-head"><strong>' +
          escapeHtml(String(leg.kind || "transfer").replace(/_/g, " ")) +
          "</strong><span>" +
          escapeHtml(minutes) +
          "</span></div>" +
          '<p class="leg-meta">' +
          escapeHtml(fromName + " -> " + toName) +
          "</p></li>"
        );
      })
      .join("");
  }

  function renderStations(rows) {
    if (!stationsListEl) {
      return;
    }
    if (!rows.length) {
      stationsListEl.innerHTML = '<p class="station-empty">No stations match your search.</p>';
      return;
    }
    stationsListEl.innerHTML = rows
      .slice(0, 400)
      .map(function (row) {
        const lines = Array.isArray(row.lines) ? row.lines : [];
        return (
          '<article class="station-card">' +
          '<div class="station-card-head"><h3>' +
          escapeHtml(String(row.name || row.station_id || "")) +
          "</h3><p>" +
          escapeHtml(String(row.station_id || "")) +
          "</p></div>" +
          '<p class="station-borough">' +
          escapeHtml(String(row.borough || "Unknown borough")) +
          "</p>" +
          '<div class="station-lines">' +
          lines
            .map(function (line) {
              const normalized = String(line || "").toUpperCase();
              const color = routeColors[normalized] || "#2c7fb8";
              return '<span class="line-chip small" style="--line-color:' + escapeHtml(color) + '">' + escapeHtml(normalized) + "</span>";
            })
            .join("") +
          "</div></article>"
        );
      })
      .join("");
  }

  async function refreshRuntime() {
    try {
      const response = await fetch("/api/runtime");
      const payload = await response.json();
      if (!response.ok) {
        throw new Error(payload.error || "Runtime endpoint failed.");
      }
      runtimeJsonEl.textContent = JSON.stringify(payload, null, 2);
    } catch (error) {
      runtimeJsonEl.textContent = JSON.stringify({ error: String(error && error.message ? error.message : error) }, null, 2);
    }
  }

  async function submitPlan(event) {
    event.preventDefault();
    const originId = resolveStationId(fromInputEl && fromInputEl.value);
    const destinationId = resolveStationId(toInputEl && toInputEl.value);
    if (!originId || !destinationId) {
      setPlannerMessage("Please select valid origin and destination station labels from the station list.", true);
      return;
    }
    setPlannerMessage("Planning route...", false);

    const params = collectWeightParams();
    params.set("from", originId);
    params.set("to", destinationId);

    try {
      const response = await fetch("/api/routes/plan?" + params.toString());
      const payload = await response.json();
      if (!response.ok) {
        throw new Error(payload.error || "Route planning failed.");
      }
      renderSummary(payload);
      setPlannerMessage("Route planned successfully.", false);
      setActiveTab("plan");
      runtimeJsonEl.textContent = JSON.stringify(
        {
          runtime: payload.runtime_cache || {},
          graph: payload.graph || {},
        },
        null,
        2,
      );
    } catch (error) {
      setPlannerMessage(String(error && error.message ? error.message : error), true);
    }
  }

  tabButtons.forEach(function (button) {
    button.addEventListener("click", function () {
      setActiveTab(button.dataset.tab || "plan");
    });
  });

  if (planFormEl) {
    planFormEl.addEventListener("submit", submitPlan);
  }
  if (refreshRuntimeEl) {
    refreshRuntimeEl.addEventListener("click", function () {
      refreshRuntime();
      setActiveTab("runtime");
    });
  }
  if (stationSearchEl) {
    stationSearchEl.addEventListener("input", function () {
      const query = String(stationSearchEl.value || "").trim().toLowerCase();
      const filtered = !query
        ? stationRows
        : stationRows.filter(function (row) {
            return (
              String(row.name || "").toLowerCase().includes(query) ||
              String(row.station_id || "").toLowerCase().includes(query) ||
              (Array.isArray(row.lines) && row.lines.some(function (line) { return String(line).toLowerCase() === query; }))
            );
          });
      renderStations(filtered);
    });
  }

  renderStationOptions(stationRows);
  renderStations(stationRows);
  runtimeJsonEl.textContent = JSON.stringify(
    {
      runtime: bootstrap.runtime || {},
      graph: bootstrap.graph || {},
      planner_available: Boolean(bootstrap.planner_available),
      planner_error: String(bootstrap.planner_error || ""),
    },
    null,
    2,
  );

  if (!bootstrap.planner_available && bootstrap.planner_error) {
    setPlannerMessage(bootstrap.planner_error, true);
  }
})();
