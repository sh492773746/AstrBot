export interface ComponentPage {
  schema_version: 1;
  id: string;
  fields: {
    properties?: Record<string, any>;
    $defs?: Record<string, { properties?: Record<string, any> }>;
  };
}

export function componentField(page: ComponentPage | null, name: string, definition?: string) {
  const schema = definition ? page?.fields.$defs?.[definition] : page?.fields;
  return schema?.properties?.[name] || {};
}

export function componentConstraints(page: ComponentPage | null, name: string, definition?: string) {
  const field = componentField(page, name, definition);
  return {
    minlength: field.minLength,
    maxlength: field.maxLength,
    min: field.minimum,
    max: field.maximum,
  };
}
