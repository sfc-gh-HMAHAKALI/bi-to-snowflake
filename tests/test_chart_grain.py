import sys
from pathlib import Path
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from assets.streamlit_ui.charts import ranked_frame


def test_preaggregated_preserves_tail_null_and_negative_values():
    frame = pd.DataFrame({'category': [str(i) for i in range(12)], 'average': [None, -2, *range(10)]})
    result = ranked_frame(frame, 'category', 'average', pre_aggregated=True)
    assert len(result) == 12
    assert result['average'].isna().sum() == 1
    assert -2 in result['average'].values
    assert not result['category'].str.contains('Other').any()
    pd.testing.assert_frame_equal(result.sort_values('category').reset_index(drop=True),
                                  frame.sort_values('category').reset_index(drop=True))


@pytest.mark.parametrize('value', [True, '', ' ', float('inf'), -float('inf')])
def test_preaggregated_rejects_invalid_numeric_values(value):
    frame = pd.DataFrame({'category': ['A'], 'value': [value]})
    with pytest.raises(ValueError): ranked_frame(frame, 'category', 'value', pre_aggregated=True)


def test_display_label_collisions_are_not_merged():
    frame = pd.DataFrame({'category': [None, '(Missing)'], 'value': [0.3, 0.8]})
    with pytest.raises(ValueError): ranked_frame(frame, 'category', 'value', pre_aggregated=True)


def test_preaggregated_rejects_duplicate_categories():
    frame = pd.DataFrame({'category': ['A', 'A'], 'ratio': [0.3, 0.8]})
    with pytest.raises(ValueError, match='one row'):
        ranked_frame(frame, 'category', 'ratio', pre_aggregated=True)
    assert ranked_frame(frame, 'category', 'ratio')['ratio'].iloc[0] == 1.1


def test_plotly_figure_preserves_query_values(monkeypatch):
    from assets.streamlit_ui import charts
    figures = []
    monkeypatch.setattr(charts.bim_ui, 'section', lambda *args: None)
    monkeypatch.setattr(charts.bim_ui, 'guard', lambda *args, **kwargs: False)
    monkeypatch.setattr(charts.filters, 'active', lambda: {})
    monkeypatch.setattr(charts.compat, 'plotly_chart', lambda figure, **kwargs: figures.append(figure))
    frame = pd.DataFrame({'category': ['negative', 'missing', 'ratio'], 'value': [-2, None, 0.3]})
    charts.ranked_bar(frame, 'category', 'value', title='Fictional', key='test', pre_aggregated=True)
    figure = figures[0]
    values = dict(zip(figure.data[0].y, figure.data[0].x))
    assert values['negative'] == -2 and values['ratio'] == 0.3
    assert pd.isna(values['missing'])
    assert figure.layout.xaxis.range is None
    assert '--' in figure.data[0].text


def test_chart_import_does_not_authenticate():
    import inspect
    from assets.streamlit_ui import compat
    assert 'st.connection(' not in inspect.getsource(compat._in_snowsight)
