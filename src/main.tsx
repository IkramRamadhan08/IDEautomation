import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./app/App.tsx";

// App has its own styling
createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>
);
