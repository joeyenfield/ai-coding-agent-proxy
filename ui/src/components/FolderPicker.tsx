import { useEffect, useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, type FleetMachine } from "../api";
import { Icon } from "./Icon";

/**
 * Choose a folder on a machine, inside its launch folders: open the folders it lists, go back up
 * (never past a launch folder), and with allowNew name a new folder to make inside. The machine
 * lists only folders, only inside its launch folders, and checks that itself.
 */
export function FolderPicker({ machine, onChange, allowNew = false }: {
  machine: FleetMachine;
  onChange: (path: string) => void;
  allowNew?: boolean;
}) {
  const [current, setCurrent] = useState(machine.roots[0] ?? "");
  const [fresh, setFresh] = useState("");
  const separator = current.includes("\\") ? "\\" : "/";
  const root = machine.roots.find((item) => current === item || current.startsWith(item.replace(/[\\/]+$/, "") + separator)) ?? machine.roots[0] ?? "";
  const listing = useQuery({
    queryKey: ["machine-folders", machine.id, current],
    queryFn: () => api.machineFolders(machine.id, current),
    enabled: machine.connected && Boolean(current),
    staleTime: 10_000,
    retry: false,
  });
  // The folder chosen: the one open, or a new one named inside it.
  const chosen = useMemo(() => {
    const sub = allowNew ? fresh.trim().replace(/^[\\/]+|[\\/]+$/g, "") : "";
    return sub ? `${current.replace(/[\\/]+$/, "")}${separator}${sub.replace(/[\\/]+/g, separator)}` : current;
  }, [allowNew, current, fresh, separator]);
  useEffect(() => onChange(chosen), [chosen, onChange]);

  // The crumbs from the launch folder down to the open one, each a way back to it.
  const crumbs = useMemo(() => {
    const below = current.slice(root.replace(/[\\/]+$/, "").length).split(/[\\/]+/).filter(Boolean);
    return below.map((name, index) => ({ name, path: root.replace(/[\\/]+$/, "") + separator + below.slice(0, index + 1).join(separator) }));
  }, [current, root, separator]);
  const open = (path: string) => {
    setCurrent(path);
    setFresh("");
  };

  return (
    <div className="folder-picker">
      <div className="folder-picker-head">
        {machine.roots.length > 1 ? (
          <select aria-label="Launch folder" value={root} onChange={(event) => open(event.target.value)}>
            {machine.roots.map((item) => (
              <option key={item} value={item}>
                {item}
              </option>
            ))}
          </select>
        ) : (
          <button type="button" className="link-button mono" onClick={() => open(root)} disabled={current === root}>
            {root}
          </button>
        )}
        {crumbs.map((crumb) => (
          <span key={crumb.path} className="folder-crumb">
            <span className="part-sep" aria-hidden="true">›</span>
            <button type="button" className="link-button mono" onClick={() => open(crumb.path)} disabled={crumb.path === current}>
              {crumb.name}
            </button>
          </span>
        ))}
        {listing.data?.parent && (
          <button type="button" className="small folder-up" onClick={() => open(listing.data.parent as string)} title="The folder above">
            <Icon name="up" size={14} />
            Up
          </button>
        )}
      </div>
      {listing.isPending && machine.connected ? (
        <p className="subtle folder-note">Asking {machine.name} for its folders…</p>
      ) : listing.isError ? (
        <p className="subtle folder-note">
          {machine.name} didn't list its folders ({listing.error.message}).
          {allowNew ? " You can still type a folder below." : ""}
        </p>
      ) : listing.data && listing.data.folders.length === 0 ? (
        <p className="subtle folder-note">No folders inside.</p>
      ) : (
        <ul className="folder-list" aria-label={`Folders in ${current}`}>
          {(listing.data?.folders ?? []).map((folder) => (
            <li key={folder.path}>
              <button type="button" onClick={() => open(folder.path)}>
                <Icon name="folder" size={16} />
                <span>{folder.name}</span>
              </button>
            </li>
          ))}
        </ul>
      )}
      {listing.data?.truncated && <p className="subtle folder-note">Only the first {listing.data.folders.length} are shown.</p>}
      {allowNew && (
        <label className="field">
          <span className="field-label">
            New folder inside <span className="optional">optional</span>
          </span>
          <input value={fresh} placeholder="my-project" onChange={(event) => setFresh(event.target.value)} />
        </label>
      )}
      <small className="subtle">
        Starts in <span className="mono">{chosen}</span>
        {allowNew && fresh.trim() && " (made there if it doesn't exist yet)"}
      </small>
    </div>
  );
}
