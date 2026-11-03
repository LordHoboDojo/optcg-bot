import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from "react";
import { api, type Me } from "./api";

type Session = { me: Me | null; refresh: () => Promise<void> };
const Ctx = createContext<Session>({ me: null, refresh: async () => {} });

export function SessionProvider({ children }: { children: ReactNode }) {
  const [me, setMe] = useState<Me | null>(null);
  const refresh = useCallback(async () => {
    try { setMe(await api.me()); } catch { setMe({ user: null, signin: null, game: null }); }
  }, []);
  useEffect(() => { refresh(); }, [refresh]);
  return <Ctx.Provider value={{ me, refresh }}>{children}</Ctx.Provider>;
}

export const useSession = () => useContext(Ctx);
