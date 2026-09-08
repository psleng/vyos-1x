<!-- include start from serial/service/utils/tls-port.xml.i -->
<node name="tls">
  <properties>
    <help>Enable TLS for TCP connections</help>
  </properties>
  <children>
    <leafNode name="template">
      <properties>
        <help>TLS template name</help>
        <valueHelp>
          <format>txt</format>
          <description>Name of TLS template defined in global-parameters</description>
        </valueHelp>
        <completionHelp>
          <path>service serial global-parameters tls template</path>
        </completionHelp>
      </properties>
    </leafNode>
  </children>
</node>
<!-- include end -->
