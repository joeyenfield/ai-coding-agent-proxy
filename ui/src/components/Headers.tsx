import type { HttpExchange } from "../api";

/** Request and response headers of an intercepted exchange. Credentials arrive already redacted. */
export function HeadersView({ http }: { http: HttpExchange }) {
  return (
    <div className="headers-view">
      <p className="mono">
        <strong>{http.method}</strong> {http.url}
      </p>
      <h3>Request headers</h3>
      <HeaderTable headers={http.request_headers} />
      <h3>
        Response headers{" "}
        {http.status != null && (
          <span className={`pill ${http.status >= 400 ? "bad" : "good"}`}>
            {http.status} {http.reason}
          </span>
        )}
      </h3>
      {http.response_headers ? <HeaderTable headers={http.response_headers} /> : <p className="subtle">Waiting for the response.</p>}
    </div>
  );
}

function HeaderTable({ headers }: { headers: [string, string][] }) {
  if (!headers.length) return <p className="subtle">None.</p>;
  return (
    <table className="header-table">
      <tbody>
        {headers.map(([name, value], index) => (
          <tr key={`${name}-${index}`}>
            <th scope="row" className="mono">
              {name}
            </th>
            <td className="mono">{value}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
