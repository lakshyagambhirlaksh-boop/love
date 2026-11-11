# Delta Options Live Dashboard

Local Streamlit dashboard for Delta Exchange India BTC and XAUT options.

## Start

Run `start-dashboard.bat`, or from PowerShell:

```powershell
python -m pip install -r requirements.txt
python -m streamlit run app.py
```

Open the local URL Streamlit prints, usually `http://localhost:8501`.

The app reads `DELTA_API_KEY` and `DELTA_API_SECRET` from environment variables, `.env`, or Streamlit secrets. Locally, copy `.streamlit/secrets.toml.example` to `.streamlit/secrets.toml` and enter your credentials there, or use `.env.example`. Keep credentials private; `.env` and `.streamlit/secrets.toml` are excluded from Git. On Streamlit Community Cloud, enter the same TOML values in the app's Secrets settings.

## Strategy behavior

- Select BTC, XAUT and expiry, then choose the same ATM strike for call and put, or symmetric paired strikes from ±1 through ±3 around ATM.
- Entry submits separate market sell orders for the call and put at the scheduled IST start time.
- Entry premium is the sum of the two actual average fill prices.
- Exit monitoring uses only the sum of the two current best asks. It closes when the ask reaches twice the entry credit (stop) or the configured fraction of entry credit (default 0.50, take profit).
- BTC and XAUT have independent scheduled close dates and times. Updating the schedule changes the deadline only; if an exit order has already started, the deadline cannot cancel it. Choose a future date and time, or use **Stop & close** to exit that asset immediately.
- While one asset is running, the other can be configured and started as a separate live trade with its own close time. An explicit per-asset confirmation is required; the additional entry uses the active strategy's quantity, take-profit and stop-loss settings.
- The manual stop closes only positions tracked by this worker. The per-asset **Close now** controls close that asset while leaving the other segment active.
- API connection checking and positions conflict checking are authenticated read operations. Orders are sent only when the user checks the live-trading acknowledgement and presses **Start live trade**.
- The dashboard worker runs in the Streamlit server process. If that process stops, its monitoring stops too.
- Market chains refresh in a background poller; quote tables and running metrics update in place, so scheduled close controls are not interrupted by full-page refreshes.

The paired option orders are separate and can fill at different times. A call/put straddle is not a continuously delta-hedged position. The app uses a reduce-only market order to unwind filled legs if a paired entry fails, and retries managed exits when a close only partially fills.
