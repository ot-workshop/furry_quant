import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import { ThemeProvider } from "./app/theme";
import "./styles.css";

const rootElement = document.getElementById("root");
if (!rootElement) throw new Error("未找到应用挂载节点 #root");

createRoot(rootElement).render(
  <StrictMode>
    <ThemeProvider>
      <App />
    </ThemeProvider>
  </StrictMode>,
);
