import React from "react";
import ReactDOM from "react-dom/client";

import { OnboardingApp } from "./routes/Welcome";

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <OnboardingApp />
  </React.StrictMode>,
);
