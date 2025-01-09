<!-- include start from rip/access-list.xml.i -->
<node name="access-list">
  <properties>
    <help>Access-list</help>
  </properties>
  <children>
    <leafNode name="in">
      <properties>
        <help>Access list to apply to input packets</help>
        <completionHelp>
          <path>policy access-list6</path>
        </completionHelp>
        <valueHelp>
          <format>txt</format>
          <description>Name of IPv6 access-list</description>
        </valueHelp>
      </properties>
    </leafNode>
    <leafNode name="out">
      <properties>
        <help>Access list to apply to output packets</help>
        <completionHelp>
          <path>policy access-list6</path>
        </completionHelp>
        <valueHelp>
          <format>txt</format>
          <description>Name of IPv6 access-list</description>
        </valueHelp>
      </properties>
    </leafNode>
  </children>
</node>
<!-- include end -->
