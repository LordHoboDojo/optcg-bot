import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter, Route, Routes } from "react-router-dom";
import Layout from "./components/Layout";
import Account from "./pages/Account";
import Analysis from "./pages/Analysis";
import Bots from "./pages/Bots";
import Docs from "./pages/Docs";
import Home from "./pages/Home";
import NotFound from "./pages/NotFound";
import Play from "./pages/Play";
import SignIn from "./pages/SignIn";
import { SessionProvider } from "./session";
import "./styles.css";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <SessionProvider>
      <BrowserRouter>
        <Routes>
          <Route element={<Layout />}>
            <Route index element={<Home />} />
            <Route path="play" element={<Play />} />
            <Route path="analysis" element={<Analysis />} />
            <Route path="account" element={<Account />} />
            <Route path="bots" element={<Bots />} />
            <Route path="docs" element={<Docs />} />
            <Route path="docs/bot-api" element={<Docs />} />
            <Route path="signin" element={<SignIn />} />
            <Route path="*" element={<NotFound />} />
          </Route>
        </Routes>
      </BrowserRouter>
    </SessionProvider>
  </StrictMode>,
);
