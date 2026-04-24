{% macro unprocessed_files(source_name, s3_prefix) %}
  (
    select distinct input_file_name() as object_key
    from json.`{{ s3_prefix }}`
    where input_file_name() not in (
      select object_key
      from {{ ref('file_watermark') }}
      where source_name = '{{ source_name }}'
    )
  )
{% endmacro %}

{% macro mark_files_processed(source_name) %}
  -- Implemented later when Bronze models are added.
{% endmacro %}

