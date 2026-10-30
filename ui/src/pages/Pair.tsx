import { useEffect } from "react";
import { Navigate } from "react-router-dom";

export function PairPage() {
  useEffect(() => {
    window.history.replaceState(null, "", "/pair");
  }, []);
  return <Navigate to="/terminals" replace />;
}
