import React from "react";
import ReactDOM from "react-dom/client";
import { HashRouter } from "react-router-dom";

import App from "./App";
import "./index.css";

/**
 * We use HashRouter rather than BrowserRouter because the existing FastAPI
 * dashboard mount only serves `/dashboard` (and `/dashboard/static/*`).
 * A hash router puts the route after `#` and never touches the server, so
 * deep links such as `/dashboard/#/accounts` reload safely without needing
 * an SPA-fallback rule on the backend.
 */
const rootEl = document.getElementById("root");
if (!rootEl) throw new Error("missing #root");

ReactDOM.createRoot(rootEl).render(
  <React.StrictMode>
    <HashRouter>
      <App />
    </HashRouter>
  </React.StrictMode>,
);
