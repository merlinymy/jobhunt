import { Link } from "react-router-dom";
import { EmptyState } from "../components/ui";

export default function NotFound() {
  return (
    <div className="mx-auto max-w-2xl pt-10">
      <EmptyState
        title="No such page."
        hint={
          <Link to="/" className="text-accent hover:underline">
            Back to the pipeline
          </Link>
        }
      />
    </div>
  );
}
