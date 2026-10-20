/* Query contract for generated pages. Populate from app-queries.json.
 * There is deliberately no destination fallback or embedded subject model.
 * SQL is authored by the offline generator, never by browser input.
 */
export type Dataset = {
  name: string;
  queries: {
    name: string;
    dimension: string | null;
    view: string;
    sql: string;
    metrics: string[];
  }[];
};

export const QUERIES: Dataset[] = [];
